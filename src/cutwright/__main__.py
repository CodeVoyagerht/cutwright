"""让 `python -m cutwright` 等价于 `cutwright`。"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
