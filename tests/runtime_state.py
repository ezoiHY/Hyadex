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
