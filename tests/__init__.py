"""HyadexServer tests; run with unittest discovery from the server directory."""

import os

# Runtime fixtures use the two supported providers.
os.environ.setdefault("HYADEX_ENABLED_BACKENDS", "claude,codex")
