#!/usr/bin/env python3
"""Entry point for the gateway and execution services.

Installed services and scripts run this file by path; the implementation is
hyadex.ops.execution.service.
"""
from hyadex.ops.execution.service import main

if __name__ == "__main__":
    raise SystemExit(main())
