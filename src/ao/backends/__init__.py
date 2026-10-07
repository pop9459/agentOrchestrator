"""Backend registry: `BackendConfig.type` → factory."""

from collections.abc import Callable

from ao.backends.base import Backend, BackendError
from ao.config import BackendConfig

Factory = Callable[[str, BackendConfig], Backend]

_FACTORIES: dict[str, Factory] = {}
_PLANNED = {"openai_compat": "KAP-80", "cli_template": "KAP-81"}


def register(backend_type: str) -> Callable[[Factory], Factory]:
    def decorator(factory: Factory) -> Factory:
        _FACTORIES[backend_type] = factory
        return factory

    return decorator


def get_backend(name: str, config: BackendConfig) -> Backend:
    factory = _FACTORIES.get(config.type)
    if factory is None:
        if ticket := _PLANNED.get(config.type):
            raise BackendError(f"backend type {config.type!r} is not implemented yet ({ticket})")
        raise BackendError(f"unknown backend type {config.type!r}")
    return factory(name, config)


# Built-in backends register themselves on import (kept last: they import `register`).
from ao.backends import claude_code  # noqa: E402, F401
