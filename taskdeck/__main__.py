"""`python3 -m taskdeck` entry point."""
import sys

from taskdeck.app import main

# Guarded so importing taskdeck.__main__ (coverage tools, runpy edge cases)
# can never launch the GUI as a side effect.
if __name__ == "__main__":
    sys.exit(main())
