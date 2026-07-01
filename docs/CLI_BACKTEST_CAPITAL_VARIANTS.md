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
python cli/run.py tsl18 backtest
```

This prints or runs the existing TSL18 backtest sections, with each 50K report
followed by the matching 5K report.
