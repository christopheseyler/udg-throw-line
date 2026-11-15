"""`python3 -m udg_throw_line` runs the service (the CLI is `python3 -m udg_throw_line.cli`)."""

import sys

from .service import main

sys.exit(main())
