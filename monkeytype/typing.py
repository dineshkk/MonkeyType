# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
from collections import defaultdict
import inspect
import types
from typing import (
    Any,
    Callable,
    Dict,
    DefaultDict,
    Generator,
    Iterable,
    Iterator,
    List,
    Set,
    Tuple,
    Type,
    Union,
)
from monkeytype.compat import is_any, is_generic, is_generic_of, is_union, name_of_generic

from mypy_extensions import TypedDict, _TypedDictMeta


DUMMY_TYPED_DICT_NAME = 'DUMMY_NAME'


# Functions like shrink_types and get_type construct new types at runtime.
# Mypy cannot currently type these functions, so the type signatures for this
# file live in typing.pyi.


def is_typed_dict(typ: type) -> bool:
    """Test indirectly using _TypedDictMeta because TypedDict does not support `isinstance`."""
    return isinstance(typ, _TypedDictMeta)


def is_anonymous_typed_dict(typ: type) -> bool:
    """Return true if this is an anonymous TypedDict as generated by MonkeyType."""
    return is_typed_dict(typ) and typ.__name__ == DUMMY_TYPED_DICT_NAME


def __are_typed_dict_types_equal(type1: type, type2: type) -> bool:
    """Return true if the two TypedDicts are equal.
    Doing this explicitly because
    TypedDict('Foo', {'a': int}) != TypedDict('Foo', {'a': int})."""

    return is_typed_dict(type2) and (type1.__name__ == type2.__name__
                                     and type1.__total__ == type2.__total__
                                     and type1.__annotations__ == type2.__annotations__)


# HACK: MonkeyType monkey-patches _TypedDictMeta!
# We need this to compare TypedDicts recursively.
_TypedDictMeta.__eq__ = __are_typed_dict_types_equal


def typed_dict_to_dict(type_dict: type) -> type:
    """Convert a TypedDict to a Dict.
    The keys of a TypedDict that we have constructed will always be strings.
    However, if the TypedDict is empty, we return the key type as Any since we
    can't justify a key type."""
    if type_dict.__annotations__ == {}:
        return Dict[Any, Any]
    return Dict[str, shrink_types(type_dict.__annotations__.values())]


def shrink_typed_dict_types(types: List[type]) -> type:
    """Shrink the TypedDicts to a single TypedDict *only* if they are all the same.
    Doing this manually because Union[TypedDict('Foo', {'a': int}),
    TypedDict('Foo', {'a': int})] doesn't return a single TypedDict.

    If there is any mismatch, fall back to a Union of the Dicts."""
    assert len(types) != 0
    first_type_dict = types[0]
    if all(typ == first_type_dict for typ in types[1:]):
        return first_type_dict
    else:
        return Union[tuple(typed_dict_to_dict(typ) for typ in types)]


def shrink_types(types):
    """Return the smallest type equivalent to Union[types].
    If all the types are TypedDicts, shrink them ourselves.
    Otherwise, turn the TypedDicts into Dicts. Union will handle deduplicating
    types (both by equality and subtype relationships)."""
    types = tuple(types)
    if len(types) == 0:
        return Any
    if all(is_typed_dict(typ) for typ in types):
        return shrink_typed_dict_types(types)
    all_dict_types = tuple(typed_dict_to_dict(typ)
                           if is_typed_dict(typ)
                           else typ
                           for typ in types)
    return Union[all_dict_types]


def make_iterator(typ):
    return Iterator[typ]


def make_generator(yield_typ, send_typ, return_typ):
    return Generator[yield_typ, send_typ, return_typ]


_BUILTIN_CALLABLE_TYPES = (
    types.FunctionType,
    types.LambdaType,
    types.MethodType,
    types.BuiltinMethodType,
    types.BuiltinFunctionType,
)


def get_dict_type(dct, max_typed_dict_size=None):
    """Return a TypedDict for `dct` if all the keys are strings.
    Else, default to the union of the keys and of the values."""
    if (all(isinstance(k, str) for k in dct.keys())
            and (max_typed_dict_size is None or len(dct) <= max_typed_dict_size)):
        return TypedDict(DUMMY_TYPED_DICT_NAME, {k: get_type(v) for k, v in dct.items()})
    else:
        key_type = shrink_types(get_type(k) for k in dct.keys())
        val_type = shrink_types(get_type(v) for v in dct.values())
        return Dict[key_type, val_type]


def get_type(obj, max_typed_dict_size=None):
    """Return the static type that would be used in a type hint"""
    if isinstance(obj, type):
        return Type[obj]
    elif isinstance(obj, _BUILTIN_CALLABLE_TYPES):
        return Callable
    elif isinstance(obj, types.GeneratorType):
        return Iterator[Any]
    typ = type(obj)
    if typ is list:
        elem_type = shrink_types(get_type(e) for e in obj)
        return List[elem_type]
    elif typ is set:
        elem_type = shrink_types(get_type(e) for e in obj)
        return Set[elem_type]
    elif typ is dict:
        return get_dict_type(obj, max_typed_dict_size)
    elif typ is defaultdict:
        key_type = shrink_types(get_type(k) for k in obj.keys())
        val_type = shrink_types(get_type(v) for v in obj.values())
        return DefaultDict[key_type, val_type]
    elif typ is tuple:
        return Tuple[tuple(get_type(e) for e in obj)]
    return typ


NoneType = type(None)
NotImplementedType = type(NotImplemented)
mappingproxy = type(range.__dict__)


def _get_union_type_str(t):
    elem_types = t.__args__
    if NoneType in elem_types:
        # Optionals end up as Union[NoneType, ...], convert it back to
        # Optional[...]
        elem_type_strs = [
            get_type_str(e) for e in elem_types if e is not NoneType]
        return 'typing.Optional[' + ','.join(elem_type_strs) + ']'
    return str(t)


def get_type_str(t):
    mod = t.__module__
    if mod == 'typing':
        if is_union(t):
            s = _get_union_type_str(t)
        else:
            s = str(t)
        return s
    elif mod == 'builtins':
        return t.__qualname__
    return mod + '.' + t.__qualname__


class TypeRewriter:
    """TypeRewriter provides a visitor for rewriting parts of types"""

    def _rewrite_container(self, cls, container):
        if container.__module__ != "typing":
            return container
        args = getattr(container, '__args__', None)
        if args is None:
            return container
        elif args == ((),):  # special case of empty tuple `Tuple[()]`
            elems = ()
        else:
            elems = tuple(self.rewrite(elem) for elem in container.__args__)
        return cls[elems]

    def rewrite_Dict(self, dct):
        return self._rewrite_container(Dict, dct)

    def rewrite_List(self, lst):
        return self._rewrite_container(List, lst)

    def rewrite_Set(self, st):
        return self._rewrite_container(Set, st)

    def rewrite_Tuple(self, tup):
        return self._rewrite_container(Tuple, tup)

    def rewrite_Union(self, union):
        return self._rewrite_container(Union, union)

    def generic_rewrite(self, typ):
        """Fallback method when there isn't a type-specific rewrite method"""
        return typ

    def rewrite(self, typ):
        if is_any(typ):
            typname = 'Any'
        elif is_union(typ):
            typname = 'Union'
        elif is_generic(typ):
            typname = name_of_generic(typ)
        else:
            typname = getattr(typ, '__name__', None)
        rewriter = getattr(
            self, 'rewrite_' + typname, None) if typname else None
        if rewriter:
            return rewriter(typ)
        return self.generic_rewrite(typ)


class RemoveEmptyContainers(TypeRewriter):
    """Remove redundant, empty containers from union types.

    Empty containers are typed as C[Any] by MonkeyType. They should be removed
    if there is a single concrete, non-null type in the Union. For example,

        Union[Set[Any], Set[int]] -> Set[int]

    Union[] handles the case where there is only a single type left after
    removing the empty container.
    """

    def _is_empty(self, typ):
        args = getattr(typ, '__args__', [])
        return args and all(is_any(e) for e in args)

    def rewrite_Union(self, union):
        elems = tuple(
            self.rewrite(e) for e in union.__args__ if not self._is_empty(e))
        if elems:
            return Union[elems]
        return union


class RewriteConfigDict(TypeRewriter):
    """Union[Dict[K, V1], ..., Dict[K, VN]] -> Dict[K, Union[V1, ..., VN]]"""

    def rewrite_Union(self, union):
        key_type = None
        value_types = []
        for e in union.__args__:
            if not is_generic_of(e, Dict):
                return union
            key_type = key_type or e.__args__[0]
            if key_type != e.__args__[0]:
                return union
            value_types.extend(e.__args__[1:])
        return Dict[key_type, Union[tuple(value_types)]]


class RewriteLargeUnion(TypeRewriter):
    """Rewrite Union[T1, ..., TN] as Any for large N."""

    def __init__(self, max_union_len: int = 5):
        super().__init__()
        self.max_union_len = max_union_len

    def _rewrite_to_tuple(self, union):
        """Union[Tuple[V, ..., V], Tuple[V, ..., V], ...] -> Tuple[V, ...]"""
        value_type = None
        for t in union.__args__:
            if not is_generic_of(t, Tuple):
                return None
            value_type = value_type or t.__args__[0]
            if not all(vt is value_type for vt in t.__args__):
                return None
        return Tuple[value_type, ...]

    def rewrite_Union(self, union):
        if len(union.__args__) <= self.max_union_len:
            return union

        rw_union = self._rewrite_to_tuple(union)
        if rw_union is not None:
            return rw_union

        try:
            for ancestor in inspect.getmro(union.__args__[0]):
                if (
                    ancestor is not object and
                    all(issubclass(t, ancestor) for t in union.__args__)
                ):
                    return ancestor
        except (TypeError, AttributeError):
            pass
        return Any


class ChainedRewriter(TypeRewriter):
    def __init__(self, rewriters: Iterable[TypeRewriter]) -> None:
        self.rewriters = rewriters

    def rewrite(self, typ):
        for rw in self.rewriters:
            typ = rw.rewrite(typ)
        return typ


class NoOpRewriter(TypeRewriter):
    def rewrite(self, typ):
        return typ


class RewriteGenerator(TypeRewriter):
    """Returns an Iterator, if the send_type and return_type of a Generator is None"""

    def rewrite_Generator(self, typ):
        args = typ.__args__
        if args[1] is NoneType and args[2] is NoneType:
            return Iterator[args[0]]
        return typ


DEFAULT_REWRITER = ChainedRewriter((
    RemoveEmptyContainers(),
    RewriteConfigDict(),
    RewriteLargeUnion(),
    RewriteGenerator(),
))
