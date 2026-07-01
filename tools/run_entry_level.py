#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trading.engine as engine  # noqa: E402
from trading.engine.execution.mt5_executor_trailing_partial import Mt5Executor  # noqa: E402
from tools.auto_explicit import main  # noqa: E402

engine.Mt5Executor = Mt5Executor

if __name__ == "__main__":
    raise SystemExit(main())
