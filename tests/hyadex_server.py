"""The whole server addressed by name, for tests.

Server modules import each other's names directly, so patching a name in one
module leaves the copies in the others untouched. Tests read any server name
here (``hyadex_server.STORE``, ``hyadex_server.start_turn``) and patch it with
``patch.object(hyadex_server, NAME, value)``: an assignment here replaces the
object in every ``hyadex`` module that holds it, and restoring puts the
original back everywhere.

Names resolve to the module that defines them; the application module's
bindings win where several modules define the same name (``router``).
Globals a module rebinds at run time are left out: patch their owning module.
"""
from __future__ import annotations

import ast
import glob  # noqa: F401  (tests patch these through the namespace)
import io  # noqa: F401
import sys
import types
from pathlib import Path
from typing import Any
from urllib.parse import quote  # noqa: F401

from fastapi.responses import StreamingResponse  # noqa: F401
from starlette.datastructures import Headers  # noqa: F401

import hyadex.main  # noqa: F401  (imports every server module)
from hyadex.core.wiring import WIRING

ROOT = Path(__file__).resolve().parents[1]


def _server_modules() -> list[types.ModuleType]:
    return [module for name, module in sorted(sys.modules.items())
            if name.startswith("hyadex.") and isinstance(module, types.ModuleType)
            and getattr(module, "__file__", None)]


def _top_level(module: types.ModuleType) -> tuple[set[str], set[str]]:
    """Names a module defines at top level, and names it rebinds with ``global``."""
    tree = ast.parse(Path(module.__file__).read_text())
    defined, rebound = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            rebound.update(node.names)
    return defined, rebound


MODULES = tuple(_server_modules())


# Names several modules define with different meanings: the one tests mean.
PREFERRED = {"SYSTEM_PROMPT": "hyadex.providers.instructions"}


def _namespace() -> dict[str, Any]:
    names: dict[str, Any] = {}
    rebound_anywhere: set[str] = set()
    owners: dict[str, list[types.ModuleType]] = {}
    for module in MODULES:
        defined, rebound = _top_level(module)
        rebound_anywhere |= rebound
        for name in defined:
            owners.setdefault(name, []).append(module)
    def most_held(name: str, candidates: list[Any]) -> Any:
        # The object most server modules hold under this name.
        counts = {id(c): sum(1 for m in MODULES if m.__dict__.get(name) is c) for c in candidates}
        return max(candidates, key=lambda c: counts[id(c)])

    application = sys.modules["hyadex.main"].__dict__
    for name, modules in owners.items():
        if name in rebound_anywhere:
            continue
        if name in PREFERRED:
            names[name] = sys.modules[PREFERRED[name]].__dict__[name]
        elif name in application:
            names[name] = application[name]
        else:
            names[name] = most_held(name, [m.__dict__[name] for m in modules if name in m.__dict__])
    # Imported helpers (modules, framework classes) used across the server.
    imported: dict[str, list[Any]] = {}
    for module in MODULES:
        for name, value in module.__dict__.items():
            if not name.startswith("__") and name not in names and name not in rebound_anywhere:
                imported.setdefault(name, []).append(value)
    for name, values in imported.items():
        names[name] = most_held(name, values)
    return names


# Module globals that core code reaches through WIRING.
WIRED_NAMES = {"JOBS": "jobs", "run_codex": "run_codex", "run_claude": "run_claude"}

globals().update({name: value for name, value in _namespace().items() if name not in globals()})


class _ServerNamespace(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        missing = object()
        previous = self.__dict__.get(name, missing)
        if previous is not missing:
            for module in MODULES:
                if module.__dict__.get(name, missing) is previous:
                    module.__dict__[name] = value
            if name in WIRED_NAMES and getattr(WIRING, WIRED_NAMES[name]) is previous:
                setattr(WIRING, WIRED_NAMES[name], value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ServerNamespace
