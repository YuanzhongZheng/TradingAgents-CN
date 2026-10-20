"""Shared package exports, without importing the registry or third-party SDKs."""
from importlib import import_module


def resolve_export(name, exports, namespace):
    if name not in exports:
        raise AttributeError(f"module {namespace['__name__']!r} has no attribute {name!r}")
    module, attribute = exports[name]
    loaded = import_module(module, namespace["__name__"])
    value = getattr(loaded, attribute) if attribute else loaded
    namespace[name] = value
    return value
