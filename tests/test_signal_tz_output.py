"""--signal-tz on the scalper generator is presentation-only: a GMT+7 feed
parses to the IDENTICAL chart times as the default GMT+3 feed."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from datetime import datetime

from generate_scalper_signals import GeneratedSignal, _write_signal_file  # noqa: E402
from xauusd_trading import parse_signals_file  # noqa: E402


def _sig(t, side="BUY"):
    return GeneratedSignal(time=t, side=side, r1=4200.0, r2=4198.0, sl=4190.0,
                           tp1=4210.0, tp2=4215.0, tp3=4220.0, reason="test",
                           entry_ref=4200.0, risk=10.0, atr=1.0,
                           spread_points=20, ema_fast=4200.0, ema_mid=4199.0,
                           ema_slow=4198.0)


def test_gmt7_feed_parses_to_identical_chart_times(tmp_path):
    # 22:30 chart time crosses midnight in GMT+7 (02:30 next day) -- the
    # rollover case must land in the next day's section yet round-trip exactly.
    sigs = [_sig(datetime(2026, 6, 12, 10, 4)),
            _sig(datetime(2026, 6, 12, 22, 30), side="SELL")]
    f3, f7 = tmp_path / "tz3.txt", tmp_path / "tz7.txt"
    _write_signal_file(sigs, f3)                # default GMT+3
    _write_signal_file(sigs, f7, signal_tz=7)   # Victor-style display

    assert "GMT+3" in f3.read_text() and "GMT+7" in f7.read_text()
    assert "2026-06-13 GMT+7" in f7.read_text()  # rollover section exists

    p3 = sorted(s.signal_time_chart for s in parse_signals_file(f3))
    p7 = sorted(s.signal_time_chart for s in parse_signals_file(f7))
    assert p3 == p7 == [datetime(2026, 6, 12, 10, 4), datetime(2026, 6, 12, 22, 30)]
