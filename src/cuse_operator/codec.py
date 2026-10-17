"""Strict JSON-shaped domain codec: no coercion and no ignored fields."""
from dataclasses import MISSING, fields, is_dataclass
from typing import get_args, get_origin, get_type_hints

from . import models
from .validation import ValidationError, validate_domain

_DOMAIN_TYPES = frozenset(
    value for value in vars(models).values()
    if isinstance(value, type) and is_dataclass(value)
)


def _decode(cls, value, path):
    if hasattr(cls, "__supertype__"):
        cls = cls.__supertype__
    if cls is str:
        if type(value) is not str:
            raise ValidationError(path, "expected string")
        return value
    if get_origin(cls) is tuple:
        if type(value) is not list:
            raise ValidationError(path, "expected JSON array")
        element_type, _ = get_args(cls)
        return tuple(_decode(element_type, item, f"{path}[{i}]") for i, item in enumerate(value))
    if cls not in _DOMAIN_TYPES:
        raise TypeError(f"unsupported domain type: {cls!r}")
    if type(value) is not dict:
        raise ValidationError(path, "expected object")
    definitions = {field.name: field for field in fields(cls)}
    for name in value:
        if name not in definitions:
            raise ValidationError(f"{path}.{name}", "unknown field")
    hints = get_type_hints(cls)
    decoded = {}
    for name, field in definitions.items():
        child_path = f"{path}.{name}"
        if name not in value:
            if field.default is MISSING and field.default_factory is MISSING:
                raise ValidationError(child_path, "missing required field")
            continue
        decoded[name] = _decode(hints[name], value[name], child_path)
    return cls(**decoded)


def from_dict(cls, value):
    """Parse one domain object and validate all locally available relationships."""
    if cls not in _DOMAIN_TYPES:
        raise TypeError("from_dict requires a canonical domain dataclass type")
    result = _decode(cls, value, "$")
    validate_domain(result)
    return result


def to_dict(obj):
    """Validate and serialize a domain object to JSON-compatible dictionaries/lists."""
    if type(obj) not in _DOMAIN_TYPES:
        raise TypeError("to_dict requires a canonical domain dataclass instance")
    validate_domain(obj)

    def encode(value):
        if is_dataclass(value):
            return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
        if type(value) is tuple:
            return [encode(item) for item in value]
        return value

    return encode(obj)


def architecture_from_dict(value):
    return from_dict(models.Architecture, value)
