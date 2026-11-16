"""Executable entry point for the Windows distribution."""

import sys
from questguard.gui import main

raise SystemExit(main(smoke_test=sys.argv[1:] == ["--smoke-test"]))
