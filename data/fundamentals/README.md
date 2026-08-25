# Fundamentals — book value per share for the valuation strategies (`val_*`)

The `val_*` strategies run mean-reversion on **price-to-book** instead of price. Questrade's
securities API exposes no book value (only current eps/pe/marketCap/shares snapshots), and a
*constant* book value makes P/B just a scaled price — so `val_*` would collapse to their price
twins. Supply book value here as a **quarterly time series** per symbol to give them a real
signal.

## File format

One CSV per traded symbol, named with dots in the ticker replaced by underscores:

```
data/fundamentals/RY_TO.csv     # for RY.TO
data/fundamentals/QQQ.csv       # for QQQ
```

Columns `date,bvps`:

```
date,bvps
2024-03-31,42.10
2024-06-30,43.05
2024-09-30,43.60
```

- `date` — the quarter-end / report date.
- `bvps` — book value per share, in the same currency as the price.

Values are **forward-filled** onto the daily bars, so quarterly rows are enough.

## Behaviour

- A symbol **with** a file → `val_*` key off real price-to-book (buy when unusually cheap on P/B).
- A symbol **without** a file → P/B collapses to the price level and `val_*` fall back to their
  price twin (no change). So names light up one at a time as you add files.

Source book values from your data provider or SEC / SEDAR filings. The CSVs are per-machine
(gitignored); this README is the only tracked file in this directory.
