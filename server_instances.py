#!/usr/bin/env python3
"""Entry point for instance management (used by instances.sh).

Installed services and scripts run this file by path; the implementation is
hyadex.ops.instances.
"""
from hyadex.ops.instances import main

if __name__ == "__main__":
    raise SystemExit(main())
