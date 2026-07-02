# Runner final cap: `none` (ride past TP3) vs `tp3` (bank at TP3)

Both books run **trailing-close** (`trailing_close_distance 0.5`), so by default the
runner leg does **not** bank at its final target — it rides the trailing-close stop
past TP3 to the highest close (`--runner-final-cap none`, the deployed default). This
study re-runs the exact same six $3K backtests with **`--runner-final-cap tp3`**, which
restores the broker TP so the leg **banks at TP3** instead of riding past it. Everything
else (feed, geometry, risk, window, capital, ticks) is identical.

- **`none`** workbooks: `reports/<name>_3K.xlsx` (the deployed default).
- **`tp3`** workbooks: `reports/<name>_3K_TP3CAP.xlsx`.

Windows: **Jul** = 2026-07-01..today (~2 days), **Jan–Jul** = 2026-01-01..today (6 months).
TICK-preferred / M1-fallback (32.7M committed ELEV8 ticks). Compounded net/equity figures
are the model's **ranking upper bound**, hypersensitive at $3K — read **drawdown %** and
**win rate**, not the headline dollars.

## Result

| Window | Book | Variant | Max DD % | Win rate | Final equity (from $3K) |
|---|---|---|---:|---:|---:|
| Jul | V073A | none | −3.08% | 50.00% | $3,274 |
| | | **tp3** | −3.08% | 50.00% | $3,274 |
| Jul | TS3K | none | **−9.23%** | 47.11% | **$4,934** |
| | | tp3 | −11.65% | 48.44% | $4,343 |
| Jul | Pooled | none | **−12.54%** | 46.88% | **$5,137** |
| | | tp3 | −15.37% | 47.73% | $4,568 |
| Jan–Jul | V073A | none | −26.21% | 55.74% | $7.39M |
| | | **tp3** | −26.18% | 57.09% | $7.88M |
| Jan–Jul | TS3K | none | **−31.75%** | 44.42% | **$301M** |
| | | tp3 | −38.02% | 45.03% | $234M |
| Jan–Jul | Pooled | none | −31.35% | 45.57% | **$654M** |
| | | tp3 | −31.40% | 46.11% | $574M |

## Read

**`none` (ride past TP3) is the better default — decisively for TS3K, neutral for V073A.**

- **TS3K** — riding past TP3 is a clear win on **both** axes: shallower drawdown
  (−31.75% vs −38.02% over Jan–Jul, ~6pp better) **and** higher net ($301M vs $234M).
  The trend-runner earns its keep.
- **V073A** — the two are ~identical (−26.21% vs −26.18% DD; net within ~7%). V073A's
  corrected-R:R TP3 is a wide **5R**, so legs rarely reach it — capping barely bites.
- **Pooled** — tracks the mix: net always higher under `none`; DD roughly flat over
  Jan–Jul, ~3pp better under `none` in July.

### Why (exit-mix mechanism, Jan–Jul)

Capping at TP3 converts runner rides into early TP3 banks:

| Exit | TS3K none | TS3K tp3 | V073A none | V073A tp3 |
|---|---:|---:|---:|---:|
| TP3 (banked at target) | 0 | **991** | 0 | **70** |
| TRAILING_STOP (rode past) | 2510 | 1927 | 967 | 908 |
| TIME_EXIT | 2123 | 1764 | 357 | 258 |
| SL | 4710 | 4671 | 954 | 856 |

For **TS3K**, 991 legs bank at TP3 under the cap instead of riding the trail — and on
net those rides (under `none`) earned *more* than the fixed TP3 while *smoothing* the
equity curve (shallower % DD). For **V073A**, only 70 legs ever reach the 5R TP3, so the
cap is nearly a no-op.

**Verdict:** keep the deployed `--runner-final-cap none`. The trailing-close runner past
TP3 is a net positive for TS3K and harmless for V073A.
