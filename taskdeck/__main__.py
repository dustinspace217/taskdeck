"""`python3 -m taskdeck` entry point."""
import sys

try:
    from taskdeck.app import main
except ImportError as exc:
    # The one dependency is the PySide6 RPM — say so instead of a raw
    # traceback, and exit non-zero so launcher scripts see the failure
    # (a zero-exit "missing dependency" would itself be a silent failure).
    print(
        f"Task Deck needs PySide6 — sudo dnf install python3-pyside6  ({exc})",
        file=sys.stderr,
    )
    sys.exit(1)

# Guarded so importing taskdeck.__main__ (coverage tools, runpy edge cases)
# can never launch the GUI as a side effect.
if __name__ == "__main__":
    sys.exit(main())
