"""Convenience launcher so the app can be started with ``python run.py``.

Adds ``src`` to ``sys.path`` so the project runs without installation.
No arguments -> GUI; with a subcommand (pull / test / points) -> CLI mode.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from data_acquirer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
