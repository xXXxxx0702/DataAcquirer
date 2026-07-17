"""Filesystem locations shared by the GUI and the CLI.

All of DataAcquirer's persisted state lives under ``<project root>/config``.
The project root is derived from the package location (``src/data_acquirer``),
which matches how the tool is normally run — from a source checkout via
``run.py`` / ``启动.bat`` or an editable install.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
PRESETS_DIR = CONFIG_DIR / "presets"
LAST_SESSION_PATH = CONFIG_DIR / "last_session.json"
BOOKMARKS_PATH = CONFIG_DIR / "bookmarks.json"
RUN_HISTORY_PATH = CONFIG_DIR / "run_history.json"
