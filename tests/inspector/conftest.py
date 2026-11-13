"""Make the standalone ``inspector`` package importable (it is not in the api image)."""
import sys
from pathlib import Path

INSPECTOR_DIR = Path(__file__).resolve().parents[2] / "inspector"
if str(INSPECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(INSPECTOR_DIR))
