"""Strategy registry (SPEC §8.3).

Built-in strategies register on import of ``bot.strategy``; user strategies are plain modules listed in
``strategy.extra_modules`` (the CLI puts ``cfg.base_dir`` on ``sys.path`` first) that use ``@register``.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from bot.errors import ConfigError
from bot.strategy.base import Strategy

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator: add ``cls`` under ``cls.name``. Empty or duplicate name -> ``ValueError``."""
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise TypeError(f"@register expects a Strategy subclass, got {cls!r}")
    if inspect.isabstract(cls):
        raise TypeError(f"cannot register abstract strategy class {cls.__qualname__}")
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"strategy class {cls.__qualname__} must define a non-empty 'name'")
    if name != name.strip():
        raise ValueError(f"strategy name {name!r} must not have surrounding whitespace")
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"duplicate strategy name '{name}': already registered by "
            f"{existing.__module__}.{existing.__qualname__}"
        )
    _REGISTRY[name] = cls
    return cls


def available_strategies() -> list[str]:
    """Registered strategy names, sorted."""
    return sorted(_REGISTRY)


def get_strategy_class(name: str) -> type[Strategy]:
    """Look up a registered strategy class; unknown name -> ``ConfigError`` listing what is available."""
    cls = _REGISTRY.get(name) if isinstance(name, str) else None
    if cls is None:
        available = ", ".join(available_strategies()) or "(none)"
        raise ConfigError(f"unknown strategy '{name}'. available: {available}")
    return cls


def create_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    """Instantiate a registered strategy (params are validated by the strategy -> ``ConfigError``)."""
    return get_strategy_class(name)(params)


def load_strategy_modules(modules: Iterable[str]) -> None:
    """Import every module so its ``@register`` decorators run.

    ``ImportError`` -> ``ConfigError``. A user module that does not even compile (``SyntaxError``) also cannot be
    imported, so it is reported the same way (clean CLI message instead of a traceback).
    """
    if isinstance(modules, str):
        modules = [modules]
    for module in modules:
        if not isinstance(module, str) or not module.strip():
            raise ConfigError(f"strategy.extra_modules entries must be non-empty module paths, got {module!r}")
        mod_name = module.strip()
        before = set(_REGISTRY)
        try:
            importlib.import_module(mod_name)
        except ImportError as exc:
            raise ConfigError(
                f"cannot import strategy module '{mod_name}': {exc} "
                "(check strategy.extra_modules; the module must be importable from the config folder)"
            ) from exc
        except SyntaxError as exc:
            raise ConfigError(
                f"cannot import strategy module '{mod_name}': syntax error in {exc.filename} "
                f"line {exc.lineno}: {exc.msg}"
            ) from exc
        added = sorted(set(_REGISTRY) - before)
        logger.info("loaded strategy module %s (new strategies: %s)", mod_name, ", ".join(added) or "none")
