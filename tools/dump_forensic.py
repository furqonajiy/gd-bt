"""Filter and pretty-print forensic.jsonl for post-mortem analysis.

Forensic logs can get large -- this tool slices them by signal_key,
event kind, cycle_id, action label, or time window, and either dumps
the full JSON of each matching event or summarizes one event per line.

Usage:
    python tools/dump_forensic.py                       # full dump
    python tools/dump_forensic.py --summary             # one-line per event
    python tools/dump_forensic.py --signal 2026-05-19#03
    python tools/dump_forensic.py --kind order_send
    python tools/dump_forensic.py --kind order_send --action modify_sl_to_tp1
    python tools/dump_forensic.py --cycle a1b2c3d4
    python tools/dump_forensic.py --since 2026-05-19T08:00:00
    python tools/dump_forensic.py --signal 2026-05-19#03 --summary

Common diagnostic recipes:

    # Did the lock action ever fire for this signal?
    python tools/dump_forensic.py --signal SIG --kind order_send \
        --action modify_sl_to_tp1 --summary

    # Did the engine ever see this signal advance to stage 1?
    python tools/dump_forensic.py --signal SIG --kind engine_snapshot --summary

    # What was MT5 showing for this signal in the last hour?
    python tools/dump_forensic.py --signal SIG --kind mt5_snapshot \
        --since 2026-05-19T08:00:00 --summary

    # Cycle-by-cycle replay of one auto iteration
    python tools/dump_forensic.py --cycle CYCLE_ID

Output: stdout (pipe through `more`, `head`, or `tee log.txt` as needed).
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(
        description="Filter and pretty-print forensic.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("path", nargs="?", default="forensic.jsonl",
                   help="Path to forensic JSONL (default: forensic.jsonl)")
    p.add_argument("--signal", default=None,
                   help="Filter by signal_key")
    p.add_argument("--kind", default=None,
                   help="Filter by event kind (cycle_start, engine_snapshot, "
                        "mt5_snapshot, reconcile_action, order_send, decision, "
                        "closure_detected, error, cycle_end)")
    p.add_argument("--cycle", default=None,
                   help="Filter by cycle_id (the 12-char prefix is fine)")
    p.add_argument("--action", default=None,
                   help="For order_send events, filter by action label "
                        "(place_pending, cancel_pending_expired, "
                        "close_catchup_tp1, modify_sl_to_tp1, close_time_exit, "
                        "cancel_after_timeout)")
    p.add_argument("--since", default=None,
                   help="ISO timestamp; show only events at or after this time")
    p.add_argument("--until", default=None,
                   help="ISO timestamp; show only events at or before this time")
    p.add_argument("--summary", action="store_true",
                   help="One-line summary per event (great for mobile)")
    p.add_argument("--raw", action="store_true",
                   help="Print raw JSONL lines (no pretty-print)")
    p.add_argument("--count", action="store_true",
                   help="Just print the count of matching events")
    args = p.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"No forensic log at {path.resolve()}", file=sys.stderr)
        return 1

    since_dt = _parse_iso(args.since) if args.since else None
    until_dt = _parse_iso(args.until) if args.until else None

    matched = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if args.signal and ev.get("signal_key") != args.signal:
            continue
        if args.kind and ev.get("kind") != args.kind:
            continue
        if args.cycle:
            cid = ev.get("cycle_id") or ""
            if not (cid == args.cycle or cid.startswith(args.cycle)):
                continue
        if args.action and ev.get("action") != args.action:
            continue
        if since_dt or until_dt:
            ts_dt = _parse_iso(ev.get("ts", ""))
            if ts_dt is None:
                continue
            if since_dt and ts_dt < since_dt:
                continue
            if until_dt and ts_dt > until_dt:
                continue
        matched += 1
        if args.count:
            continue
        if args.raw:
            print(line)
        elif args.summary:
            print(_one_line(ev))
        else:
            print(json.dumps(ev, indent=2, ensure_ascii=False))
            print()

    if args.count:
        print(matched)
    return 0


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _one_line(ev: dict) -> str:
    ts = (ev.get("ts") or "?")[:19]
    cycle = (ev.get("cycle_id") or "")[:8]
    kind = ev.get("kind") or "?"
    key = ev.get("signal_key") or ""

    if kind == "cycle_start":
        return (f"{ts} [{cycle}] CYCLE_START "
                f"{ev.get('subcommand')} iter={ev.get('iteration')} "
                f"eq=${_fmt(ev.get('equity'))} tracked={ev.get('tracked_count')} "
                f"bid={_fmt(ev.get('bid'))} ask={_fmt(ev.get('ask'))}")

    if kind == "cycle_end":
        return (f"{ts} [{cycle}] CYCLE_END "
                f"placed={ev.get('placed')} mod={ev.get('modified')} "
                f"cancel={ev.get('cancelled')} closed={ev.get('closed')} "
                f"errors={ev.get('errors')}")

    if kind == "engine_snapshot":
        statuses = " ".join(
            f"#{e['index']}={e['status']}"
            + (f"@{e['fill_time'][11:16]}" if e.get('fill_time') else "")
            for e in ev.get("entries", [])
        )
        first_fill = ev.get("first_fill_time")
        first_fill_str = first_fill[11:19] if first_fill else "-"
        deadline = ev.get("time_exit_deadline")
        deadline_str = deadline[11:16] if deadline else "-"
        return (f"{ts} [{cycle}] ENGINE   {key} stage={ev.get('stage')} "
                f"first_fill={first_fill_str} deadline={deadline_str} | "
                f"{statuses}")

    if kind == "mt5_snapshot":
        n_ord = len(ev.get("orders", []))
        positions = ev.get("positions", []) or []
        pos_str = " ".join(
            f"#{p['ticket']}@{_fmt(p['price_open'])} SL={_fmt(p['sl'])} "
            f"TP={_fmt(p['tp'])}"
            for p in positions
        )
        return (f"{ts} [{cycle}] MT5      {key} orders={n_ord} "
                f"positions={len(positions)} | {pos_str}")

    if kind == "reconcile_action":
        return (f"{ts} [{cycle}] RECONCILE {key} #{ev.get('entry_index')} "
                f"{ev.get('before_status')}→{ev.get('after_status')} "
                f"ticket={ev.get('mt5_ticket')} "
                f"@{_fmt(ev.get('fill_price'))} "
                f"(planned {_fmt(ev.get('planned_price'))})")

    if kind == "reconcile_skipped":
        return (f"{ts} [{cycle}] RECON_SKIP {key} {ev.get('reason')}")

    if kind == "order_send":
        resp = ev.get("response") or {}
        if ev.get("success"):
            outcome = f"OK ticket={resp.get('order') or resp.get('deal')}"
        else:
            outcome = (f"FAIL rc={resp.get('retcode')} "
                       f"'{(resp.get('comment') or '').strip()}'")
        req = ev.get("request") or {}
        bits = []
        if "price" in req:
            bits.append(f"price={_fmt(req['price'])}")
        if "sl" in req:
            bits.append(f"sl={_fmt(req['sl'])}")
        if "tp" in req:
            bits.append(f"tp={_fmt(req['tp'])}")
        if "position" in req:
            bits.append(f"pos={req['position']}")
        if "order" in req:
            bits.append(f"order={req['order']}")
        detail = " ".join(bits)
        return (f"{ts} [{cycle}] ORDER    {key} {ev.get('action')} "
                f"[{detail}] → {outcome}")

    if kind == "decision":
        return (f"{ts} [{cycle}] DECISION {key} → {ev.get('action')}"
                + (f"  {ev.get('rationale')}" if ev.get('rationale') else ""))

    if kind == "closure_detected":
        return (f"{ts} [{cycle}] CLOSED   {key} {ev.get('summary')} "
                f"realized=${_fmt(ev.get('realized_pnl'))}")

    if kind == "error":
        msg = ev.get("message") or ""
        return f"{ts} [{cycle}] ERROR    @{ev.get('where')}: {msg[:200]}"

    return f"{ts} [{cycle}] {kind:8s} {key}"


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        try:
            return f"{v:g}"
        except Exception:
            return str(v)
    return str(v)


if __name__ == "__main__":
    raise SystemExit(main())
