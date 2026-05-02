"""
Shim that re-exports slave/config.py for use in the test environment.

The slave code does 'from config import ...' at the top level.
When the shim path is prepended to sys.path, this file is found first
and uses importlib to load the real slave config by file path.
"""

import importlib.util
import os

_SLAVE_CONFIG = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "slave", "config.py")
)

_spec = importlib.util.spec_from_file_location("_slave_config", _SLAVE_CONFIG)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Re-export everything from the real config into this namespace
for _k, _v in vars(_mod).items():
    if not _k.startswith("__"):
        globals()[_k] = _v
