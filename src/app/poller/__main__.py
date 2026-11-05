"""Make ``python -m app.poller`` run the poller loop."""

import sys

from app.poller.main import run

if __name__ == "__main__":
    sys.exit(run())
