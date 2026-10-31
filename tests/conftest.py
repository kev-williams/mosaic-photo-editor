import os
import sys
import tempfile
from pathlib import Path

# Keep tests away from your real photos and app data.
_tmp = Path(tempfile.mkdtemp(prefix="mosaic-test-"))
os.environ["MOSAIC_PHOTOS_DIR"] = str(_tmp / "photos")
os.environ["MOSAIC_STATE_DIR"] = str(_tmp / "state")
os.environ["MOSAIC_LOG_DIR"] = str(_tmp / "logs")
os.environ["MOSAIC_AI_FAKE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
