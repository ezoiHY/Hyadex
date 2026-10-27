#!/usr/bin/env python3
"""HyadexServer entry point.

Installed services run ``agent_server.py serve`` and uvicorn loads
``agent_server:app``, so this file stays at the repository root. The server
itself lives in the ``hyadex`` package; the application is assembled in
``hyadex.main``.
"""
from hyadex.main import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
