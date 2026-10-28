import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))
os.environ.setdefault("POOL_FILE", str(ROOT / "pool.yaml"))
os.environ.setdefault("LANGFUSE_DISABLED", "1")
