#!/usr/bin/env python3
"""Generate a corrected-R:R Victor feed: rewrite TP1/TP2/TP3 to a fixed ladder.

Victor's posted TP1 risk:reward collapsed to ~0.5-0.67 through 2024-Jan 2026
(and again in July 2026) while his Feb-Jun 2026 era ran ~1.1/2.5/5. This tool
bakes a CONSISTENT asymmetric ladder into a DERIVED feed file so every signal
carries the same SL:TP ratios while everything else about the strategy --
entries, range, SL, timing, ordering -- stays exactly as posted.

The rewrite math mirrors ``trading.engine.strategy.backtest.apply_signal_rr_policy``
byte-for-byte in meaning (that function's docstring prescribes exactly this
feed-level bake for live deployability):

    entry_edge = range_high (BUY) / range_low (SELL)
    risk       = |entry_edge - SL|          (NOMINAL: the posted stop)
    TPk        = entry_edge + rk * risk (BUY)  /  entry_edge - rk * risk (SELL)

The transform is TEXT-LEVEL: only the three TP numbers on a signal line are
replaced; date headers, blank lines, signal numbering, the entry range, the SL
and the trailing clock time are preserved verbatim, so the derived feed diffs
cleanly against the raw one and parses through the identical pipeline.
``victor_signals.txt`` itself is never modified -- it stays the pristine
as-posted provider record (other Victor books keep reading it unchanged).

Usage:
  python tools/generate_victor_rr_feed.py \
    --input victor_signals.txt --rr1 2.0 --rr2 3.0 --rr3 5.0 \
    --output signals/victor_rr20x30x50.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# One signal line: "N. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM"
# TP2/TP3 are optional (rare early lines omit them). Groups capture everything
# needed to rewrite the TP numbers in place and keep the rest verbatim.
SIGNAL_RE = re.compile(
    r"^(?P<head>\s*\d+\.\s+(?P<side>BUY|SELL)\s+XAUUSD\s+"
    r"(?P<e1>\d+(?:\.\d+)?)\s*-\s*(?P<e2>\d+(?:\.\d+)?)\s+"
    r"SL\s+(?P<sl>\d+(?:\.\d+)?))"
    r"(?P<tp1>\s+TP1\s+)(?P<tp1v>\d+(?:\.\d+)?)"
    r"(?:(?P<tp2>\s+TP2\s+)(?P<tp2v>\d+(?:\.\d+)?))?"
    r"(?:(?P<tp3>\s+TP3\s+)(?P<tp3v>\d+(?:\.\d+)?))?"
    r"(?P<tail>.*)$",
    re.IGNORECASE,
)


def _fmt(x: float) -> str:
    """Feed-style number: whole -> '4093', else 2dp with trailing zeros trimmed."""
    v = round(x, 2)
    if v == int(v):
        return str(int(v))
    s = f"{v:.2f}"
    return s.rstrip("0").rstrip(".")


def rewrite_line(line: str, rr1: float, rr2: float, rr3: float,
                 max_risk: float = 30.0) -> tuple[str, bool]:
    """Rewrite one line's TPs to the ladder; (line, was_rewritten).

    A line whose |entry_edge - SL| exceeds ``max_risk`` points is left VERBATIM:
    those are provider SL typos (the wrong-hundreds ~100-pt shifts and the
    extra-digit 47802 case the listener's apply_signal_corrections repairs
    live), and rewriting TPs off a phantom 100+-pt risk would be nonsense.
    Keeping them as-posted means baseline and every ladder candidate treat the
    typo lines identically, so the A/B isolates the R:R change alone.
    """
    m = SIGNAL_RE.match(line.rstrip("\n"))
    if not m:
        return line, False
    side = m.group("side").upper()
    e1, e2, sl = float(m.group("e1")), float(m.group("e2")), float(m.group("sl"))
    entry = max(e1, e2) if side == "BUY" else min(e1, e2)   # entry_edge
    risk = abs(entry - sl)
    if risk <= 0.0 or risk > max_risk:                       # malformed/typo: keep as posted
        return line, False
    sign = 1.0 if side == "BUY" else -1.0
    out = m.group("head")
    out += m.group("tp1") + _fmt(entry + sign * rr1 * risk)
    if m.group("tp2v") is not None:
        out += m.group("tp2") + _fmt(entry + sign * rr2 * risk)
    if m.group("tp3v") is not None:
        out += m.group("tp3") + _fmt(entry + sign * rr3 * risk)
    out += m.group("tail")
    return out + "\n", True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", default="victor_signals.txt")
    p.add_argument("--output", required=True,
                   help="Derived feed path, e.g. signals/victor_rr20x30x50.txt "
                        "(dot-free ladder encoding per the artifact-name rule).")
    p.add_argument("--rr1", type=float, required=True, help="TP1 = rr1 x risk")
    p.add_argument("--rr2", type=float, required=True, help="TP2 = rr2 x risk")
    p.add_argument("--rr3", type=float, required=True, help="TP3 = rr3 x risk")
    p.add_argument("--max-risk", type=float, default=30.0,
                   help="Leave lines with |entry_edge-SL| above this VERBATIM "
                        "(provider SL typos; default 30 pts, ~3x Victor's widest "
                        "real stop).")
    args = p.parse_args(argv)
    if not (0.0 < args.rr1 < args.rr2 < args.rr3):
        raise SystemExit("ladder must satisfy 0 < rr1 < rr2 < rr3")

    src = Path(args.input)
    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = rewritten = skipped = 0
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            total += 1
            out, was_sig = rewrite_line(line, args.rr1, args.rr2, args.rr3,
                                        max_risk=args.max_risk)
            rewritten += int(was_sig)
            if not was_sig and SIGNAL_RE.match(line.rstrip("\n")):
                skipped += 1
            fout.write(out)
    print(f"[rr-feed] {dst}  lines={total} signals_rewritten={rewritten} "
          f"typo_lines_kept_verbatim={skipped} ladder={args.rr1}/{args.rr2}/{args.rr3}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
