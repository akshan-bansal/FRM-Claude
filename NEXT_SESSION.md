# Next-session backlog

Queued work, most-ready first. Each item is self-contained enough to start cold. Standing
constraints still apply: new research goes through the **walk-forward gate** before it's wired as
validated; anything that places live orders stays behind the **human go-live confirmation** (paper
/ dry-run first); use `httpx` (not `requests`); `ruff` + `mypy --strict` + `pytest` must stay green.

## 1. Cross-interface arbitrage — interlisted equities (TSX ⇄ NYSE)
The equity analog of the crypto cross-exchange arb. Arb the *same* stock trading on two venues:
Canadian interlisted names on **TSX vs NYSE**, e.g. RY.TO/RY, TD.TO/TD, ENB.TO/ENB, BMO.TO/BMO,
CNQ.TO/CNQ, SHOP.TO/SHOP. The edge is the price gap **adjusted for CAD/USD FX**, net of commissions
— that FX leg is the twist vs the crypto version.

- **Reuse:** `microstructure/cross_exchange.py::CrossExchangeArb` is the right shape; generalize it
  so one "venue" price is FX-converted before comparison, or add an `InterlistedArb` that wraps it.
- **Data:** Questrade quotes both the `.TO` and US listings; need a live CAD/USD rate (Questrade FX
  quote, or a currency ETF proxy). Build an interlisted pair map (`{TSX: NYSE}`).
- **First step:** pair map + FX-adjusted spread (`px_to * fx_usdcad` vs `px_us`) + paper detection
  when it clears commissions. Paper only.
- Note the user's phrasing was "cross-interface" — confirm they mean interlisted TSX/NYSE (this),
  not arb across our *execution interfaces* (Questrade vs QC/LEAN), before building.

## 2. Deeper crypto history → walk-forward the crypto sleeve
The crypto sleeve (`analysis/universe.py::CRYPTO_SLEEVE`: BTC, XMR, XRP, XLM, LINK, ETH) is currently
`tier="screened"` — in-sample only, because `data/kraken_ohlc.py` caps at ~720 daily bars (Kraken's
OHLC endpoint limit), too short to walk-forward.

- **Get history:** paginate Kraken `/0/public/Trades` with the `since` cursor and aggregate to daily
  bars (multi-year), or use a vendor with multi-year daily crypto. Extend `data/kraken_ohlc.py`.
- **Validate:** run the 6 sleeve pairs through the same walk-forward gate as the equity pool
  (2y train / 6mo test, per-fold re-opt, WFE ≥ 0.5 ∧ OOS>0 ∧ ≥10 trades). Use crypto annualization
  (365) in the metrics for correctness.
- **Promote:** move any survivors from `tier="screened"` to a validated tier; drop the rest.
  Remember the screen scores were weak (BTC macd 2.59 leads), so expect attrition.

## 3. KrakenBroker — let the Router fill crypto orders
The crypto sleeve routes through the Router's gates but there's no crypto execution broker, so it
can't fill. Build one so crypto goes paper → (later, gated) live.

- **Implement** `brokers/kraken.py::KrakenBroker` against the `brokers/base.py::Broker` interface
  (`accounts`, `positions`, `quote`/`quotes`, `candles`, `equity`, `place_order`, `cancel_order`).
  Public data via existing Kraken REST; orders via Kraken's **private** REST API (auth) — but keep
  live placement behind the go-live gate, PaperBroker first. Register in `brokers/__init__.py`.
- **FX/units:** crypto is fractional; `OrderIntent.shares` is `int` today — decide whether to carry
  fractional size for crypto (probably a separate size field or a scaled-units convention).
- Secrets (Kraken API key/secret) go in `.env` only, same as Questrade; never commit them.

---
_Also on the board (lower priority): wire microprice/OFI as features into the research strategies;
promote the paper A-S maker toward gated live quoting on Kraken._
