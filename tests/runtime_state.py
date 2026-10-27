"""Patch agent_server globals by name, including turn state that lives in RUNS."""
from contextlib import ExitStack
from unittest.mock import patch

from hyadex.runs import LEGACY_NAMES


def patch_server(module, name: str, value):
    attribute = LEGACY_NAMES.get(name)
    if attribute is not None:
        return patch.object(module.RUNS, attribute, value)
    return patch.object(module, name, value)


def patch_server_multiple(module, **values):
    """``patch.multiple`` for agent_server, routing turn state to RUNS."""
    runs = {LEGACY_NAMES[name]: value for name, value in values.items() if name in LEGACY_NAMES}
    others = {name: value for name, value in values.items() if name not in LEGACY_NAMES}
    stack = ExitStack()
    if runs:
        stack.enter_context(patch.multiple(module.RUNS, **runs))
    if others:
        stack.enter_context(patch.multiple(module, **others))
    return stack
