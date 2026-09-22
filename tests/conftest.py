"""
Put src on the import path so tests import modules the same way the scripts do.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def pytest_configure(config):
    """
    Import test modules by path, so files with the same name in different folders do not collide.
    """
    config.option.importmode = "importlib"
