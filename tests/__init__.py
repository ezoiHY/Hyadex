"""HyadexServer tests; run with unittest discovery from the server directory."""

import os

# Hyadex ships with in-app updates disabled. The inherited update suites still
# exercise that machinery, so they opt in here; tests of the default override it.
os.environ.setdefault("HYADEX_MANAGED_UPDATES", "1")

# Inherited provider suites cover every backend; Hyadex enables two by default.
os.environ.setdefault("HYADEX_ENABLED_BACKENDS", "claude,codex,cursor")
