from __future__ import annotations

import os

# Tests must never call an external model even when a developer has a local key.
os.environ["FLOWGUARD_AGENT_MODE"] = "deterministic"
