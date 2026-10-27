"""Patch agent_server globals by name, including state that now lives on objects."""
from contextlib import ExitStack
from unittest.mock import patch

from hyadex.core import runs, server
from hyadex.providers import state as providers

# Old module global -> (state object attribute on agent_server, field)
LEGACY = {**{name: ("RUNS", field) for name, field in runs.LEGACY_NAMES.items()},
          **{name: ("SERVER", field) for name, field in server.LEGACY_NAMES.items()},
          **{name: ("PROVIDERS", field) for name, field in providers.LEGACY_NAMES.items()}}


def patch_server(module, name: str, value):
    if name in LEGACY:
        holder, field = LEGACY[name]
        return patch.object(getattr(module, holder), field, value)
    return patch.object(module, name, value)


def patch_server_multiple(module, **values):
    """``patch.multiple`` for agent_server, routing moved state to its object."""
    stack = ExitStack()
    grouped: dict[str, dict] = {}
    for name, value in values.items():
        holder, field = LEGACY.get(name, (None, name))
        grouped.setdefault(holder, {})[field] = value
    for holder, fields in grouped.items():
        stack.enter_context(patch.multiple(getattr(module, holder) if holder else module, **fields))
    return stack


def patch_server_namespace(module, values: dict):
    """Like ``patch.dict(module.__dict__, values)``, but through the facade so
    modules split out of agent_server see the same replacements."""
    stack = ExitStack()
    for name, value in values.items():
        holder, field = LEGACY.get(name, (None, name))
        target = getattr(module, holder) if holder else module
        stack.enter_context(patch.object(target, field, value, create=not hasattr(target, field)))
    return stack


class ServerNamespace:
    """``agent_server.__dict__`` for tests that index it, writing through the
    facade so split modules see assignments. ``close`` restores them."""

    def __init__(self, module):
        self._module = module
        self._stack = ExitStack()
        self._patched: set[str] = set()

    def __getitem__(self, name):
        return getattr(self._module, name)

    def __setitem__(self, name, value):
        if name in self._patched:
            setattr(self._module, name, value)
            return
        self._patched.add(name)
        self._stack.enter_context(patch.object(self._module, name, value, create=not hasattr(self._module, name)))

    def __contains__(self, name):
        return hasattr(self._module, name)

    def get(self, name, default=None):
        return getattr(self._module, name, default)

    def close(self):
        self._stack.close()
