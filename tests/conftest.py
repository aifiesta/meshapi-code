"""Shared pytest config for the meshapi test suite.

Makes `import meshapi...` work whether the package is installed (editable /
CI `pip install -e .`) or run straight from a checkout — so `pytest` works
from the repo root with no PYTHONPATH ceremony.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
