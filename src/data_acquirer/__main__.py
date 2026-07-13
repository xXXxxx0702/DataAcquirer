"""``python -m data_acquirer`` — GUI by default, CLI mode with a subcommand."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
