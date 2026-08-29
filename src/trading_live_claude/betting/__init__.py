"""Sports-betting analytics: fair-line prediction (de-vigged consensus) and arbitrage.

The same two ideas that run through the trading side, in a new domain:

* **Prediction** — the market's own consensus, cleaned of the bookmaker margin, is a strong estimate
  of the true probability. Averaging the de-vigged lines across books gives a "fair" probability per
  outcome (:mod:`.value`), and a book quoting *better* than fair is a positive-expected-value bet,
  staked by Kelly. This is the betting analog of the consensus-fair-value trick used for the
  cross-exchange FX rate and interlisted USD/CAD.
* **Arbitrage** — taking the best price for each outcome across books, if the implied probabilities
  sum to less than 1 there is a guaranteed profit regardless of result (:mod:`.arbitrage`) — exactly
  the cross-venue edge detector, with the bookmaker overround playing the role of transaction cost.

Everything here is a pure function of odds inputs, so it is fully testable without a live feed; a
licensed odds source (Betfair Exchange API, The Odds API) plugs into these functions.
"""
from __future__ import annotations

from .arbitrage import ArbOpportunity, detect_arbitrage
from .odds import American_to_decimal, devig, implied_prob, overround
from .value import ValueBet, consensus_fair_probs, value_bets

__all__ = [
    "American_to_decimal",
    "ArbOpportunity",
    "ValueBet",
    "consensus_fair_probs",
    "detect_arbitrage",
    "devig",
    "implied_prob",
    "overround",
    "value_bets",
]
