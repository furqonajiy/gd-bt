# CLI backtest capital variants

`cli/run.py` keeps the `cli/*.txt` snapshots as the source of truth for strategy
settings and date windows.

When a selected backtest command contains both:

```text
--initial-capital 50000
--output-dir reports/<name>
```

the launcher expands it into two variants:

1. the original 50K command; and
2. a 5K clone.

The 5K clone keeps the same charts, ticks, strategy flags, start date, end date,
and month window. It changes only:

```text
--initial-capital 5000
--output-dir reports/<name>_5k
```

Example:

```text
python cli/run.py tsl18 backtest      # TSL18 self-scalper trailing ($50K book)
python cli/run.py v073a backtest      # V073A Victor corrected-R:R trailing
```

This prints or runs the existing backtest sections, with each 50K report followed
by the matching 5K report. Use `--print` to preview the commands without running.

A book authored at a **non-$50K** capital is a deliberate sizing choice and is
**left untouched** — no 5K clone, no `_5k` output dir (e.g. the live TS3K book at
`--initial-capital 3000`). The expansion only mirrors a $50K book down to $5K; it
never rewrites a strategy's authored capital. Pinned by
`tests/test_cli_run_launcher.py` (`test_backtest_keyword_prints_50k_and_5k_variants`
on TSL18, `test_non_50k_book_backtest_is_not_expanded` on TS3K) and
`tests/test_cli_run_capital_variants.py`.

## July tick data + automation

The 2026 sections (5/6) use `tools/backtest_hybrid.py` with an open-ended
`--start-date` and a glob `--ticks data/ticks/XAUUSD_TICK_*_ELEV8.csv`, so once the
**July** tick archive is committed they extend through July with **no snapshot
edit** — the month/date windows are unchanged.

The **`July V817 TSL18 Backtests + Staged Sweep`** workflow
(`.github/workflows/july-v817-tsl18-backtests-staged-sweep.yml`, **manual-only**)
runs `python cli/run.py <book> backtest` to produce both the $50K and $5K reports
from the July tick data, then a **bounded** staged
TSL18 quality/collision sweep (`smoke` → `full_recent` (jun_jul) → `validate_top`
(jan_jul)). Dispatched with `commit_results=true` it commits the backtest workbooks
+ sweep summaries back to `main`. It never uses `--execute`, never trades live, and
never promotes a strategy.
