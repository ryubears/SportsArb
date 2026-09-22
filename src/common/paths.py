"""
Where the project keeps its files.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"     # Databases, logs, and venue key files. Not committed.
