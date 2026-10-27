#!/usr/bin/env python3
"""Entry point for uninstalling the gateway/execution layout (used by uninstall.sh).

Installed services and scripts run this file by path; the implementation is
hyadex.ops.execution.uninstall.
"""
from hyadex.ops.execution.uninstall import main

if __name__ == "__main__":
    raise SystemExit(main())
