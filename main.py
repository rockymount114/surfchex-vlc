import asyncio
import sys
from pathlib import Path

# Add src/ directory to sys.path so modules inside src are importable
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from main import main as app_main


if __name__ == "__main__":
    asyncio.run(app_main())
