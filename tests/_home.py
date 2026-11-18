"""Point CLIPPER_HOME at a throwaway folder before clipper is imported."""
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="clipper-test-"))
os.environ["CLIPPER_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
