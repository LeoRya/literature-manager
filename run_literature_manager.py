#!/usr/bin/env python3
from __future__ import annotations

import sys
import faulthandler


def main() -> int:
    faulthandler.enable()
    try:
        from literature_manager.gui import run
    except ImportError as exc:
        if exc.name == "PySide6":
            print("缺少 PySide6。请先运行：python3 -m pip install -r requirements.txt", file=sys.stderr)
            return 2
        raise
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
