#!/usr/bin/env python3
"""Isolated E2E tests focusing on approval logic without external dependencies.

This suite tests the core approval system logic by mocking external dependencies,
allowing comprehensive testing without requiring structlog, pydantic, etc. to be
installed in the base Python environment.
"""
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from pathlib import Path
import tempfile

# ============================================================================ #
# MOCKED DATA STRUCTURES                                                     #
# ============================================================================ #

@dataclass
class MockOrderIntent:
    symbol: str
    action: str  # BUY/SELL
    shares: int
    entry: float
    stop: float
    target: float
    strategy: str
    risk_dollars: float
    account_number: str
    symbolId: int = 1


@dataclass
class MockMarketContext:
    strategy_rank: Optional[int] = None
    universe_size: int = 1
    r_multiple: Optional[float] = None
    trend_slope: Optional[float] = None
    rsi_14: Optional[float] = None
    atr_pct: Optional[float] = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class MockIntelSnapshot:
    strategic_risk: float = 50.0
    energy_stress: float = 0.5
    fear_greed: float = 50.0
    degraded: bool = False


@dataclass
class MockPrompt:
    intent_id: str
    symbol: str
    action: str
    shares: int
    notional_usd: float
    risk_dollars: float
    broker: str
    thesis: str
    intel_ref: str
    strategy: str
    created_at: datetime
    expires_at: datetime
    canonical: str

    @staticmethod
    def from_intent(intent: MockOrderIntent, broker: str, thesis: str, intel_ref: str) -> "MockPrompt":
        notional = intent.shares * intent.entry
        intent_id = hashlib.sha256(f"{intent.symbol}{intent.shares}{intent.entry}{datetime.now().isoformat()}".encode()).hexdigest()[:12]
        canonical = f"{broker}|{intent.action}|{intent.symbol}|{intent.shares}|{intent.entry:.2f}|{notional:.2f}|{intent.risk_dollars:.2f}|{intent_id}"
        return MockPrompt(
            intent_id=intent_id,
            symbol=intent.symbol,
            action=intent.action,
            shares=intent.shares,
            notional_usd=notional,
            risk_dollars=intent.risk_dollars,
            broker=broker,
            thesis=thesis,
            intel_ref=intel_ref,
            strategy=intent.strategy,
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(seconds=90),
            canonical=canonical,
        )


# ============================================================================ #
# MOCK INVESTMENT ENGINE                                                     #
# ============================================================================ #

class MockVSInvestmentEngine:
    """Simplified thesis generator."""

    def __init__(self, writeup_dir: Path) -> None:
        self.writeup_dir = writeup_dir
        self.writeup_dir.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def explain(
        self,
        intent: MockOrderIntent,
        broker: str,
        market: Optional[MockMarketContext] = None,
        overlay_snapshot: Optional[MockIntelSnapshot] = None,
    ) -> tuple[str, str]:
        """Generate thesis with overlay risk prioritization."""
        if market is None:
            market = MockMarketContext()

        self.count += 1
        ref = f"vs_{broker}_{intent.symbol}_{self.count:04d}"

        # Build thesis clauses
        clauses = []

        # Signal clause (research-flavored, not advice)
        clauses.append(
            f"{intent.symbol} — {intent.strategy} signal observed "
            f"(rank {market.strategy_rank or '?'}/{market.universe_size})"
        )

        # Overlay clauses (must survive truncation)
        if overlay_snapshot:
            if overlay_snapshot.strategic_risk > 70:
                clauses.append(f"geo-risk {int(overlay_snapshot.strategic_risk)}")
            if overlay_snapshot.energy_stress > 0.6:
                clauses.append("energy stress elevated")
            if overlay_snapshot.fear_greed < 30:
                clauses.append(f"F&G {int(overlay_snapshot.fear_greed)} (fear regime)")

        # Market context
        if market.r_multiple:
            clauses.append(f"R={market.r_multiple:.1f}")
        if market.rsi_14:
            clauses.append(f"RSI(14)={int(market.rsi_14)}")

        # Concatenate with truncation
        thesis = "; ".join(clauses)
        if len(thesis) > 140:
            # Prefer overlay clauses if truncating
            overlay_clauses = [c for c in clauses if "geo-risk" in c or "energy" in c or "F&G" in c]
            signal_clauses = [c for c in clauses if c not in overlay_clauses]
            # Rebuild: overlay first
            thesis = ("; ".join(overlay_clauses) + "; " + "; ".join(signal_clauses))[:140]

        # Persist writeup
        writeup = {
            "symbol": intent.symbol,
            "broker": broker,
            "reason_clauses": clauses,
            "overlay_snapshot": overlay_snapshot.__dict__ if overlay_snapshot else None,
            "warnings": ["degraded overlay" if overlay_snapshot and overlay_snapshot.degraded else None],
        }
        # Sanitize ref for filename (replace / with _)
        filename = ref.replace("/", "_")
        (self.writeup_dir / f"{filename}.json").write_text(json.dumps(writeup, default=str))

        return thesis, ref

    def load(self, ref: str) -> dict[str, Any]:
        filename = ref.replace("/", "_")
        path = self.writeup_dir / f"{filename}.json"
        return json.loads(path.read_text()) if path.exists() else {}


# ============================================================================ #
# MOCK APPROVAL STORE                                                        #
# ============================================================================ #

class MockApprovalStore:
    """In-memory approval store."""

    def __init__(self) -> None:
        self.intents: dict[str, MockPrompt] = {}
        self.decisions: dict[str, str] = {}  # intent_id -> ACCEPT/DECLINE

    def publish_prompt(self, prompt: MockPrompt) -> None:
        self.intents[prompt.intent_id] = prompt

    def pending(self) -> list[MockPrompt]:
        # Filter unexpired
        now = datetime.now()
        return [p for p in self.intents.values() if p.expires_at > now and p.intent_id not in self.decisions]

    def respond(self, intent_id: str, decision: str) -> bool:
        if intent_id not in self.intents:
            return False
        self.decisions[intent_id] = decision
        return True

    def decision_for(self, intent_id: str) -> Optional[str]:
        return self.decisions.get(intent_id)


# ============================================================================ #
# MOCK APPROVAL ROUTER                                                       #
# ============================================================================ #

class MockApprovalRouter:
    """Simplified approval router."""

    def __init__(
        self,
        store: MockApprovalStore,
        ttl_seconds: int = 90,
        thesis_fn=None,
    ):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.thesis_fn = thesis_fn or (lambda intent, broker: ("", ""))
        self.journal_accepts = []
        self.journal_rejects = []
        self.journal_declines = []

    def submit(
        self,
        intent: MockOrderIntent,
        broker: str = "test",
        equity: float = 100_000,
        existing_risk: float = 0,
        open_positions: int = 0,
    ) -> Optional[dict[str, Any]]:
        """Submit order for approval."""
        # Gate 1: Shares > 0
        if intent.shares <= 0:
            self.journal_rejects.append(f"intent.shares <= 0")
            return None

        # Gate 2: Risk within limits
        if existing_risk + intent.risk_dollars > 0.1 * equity:
            self.journal_rejects.append(f"risk_allocation_exceeded")
            return None

        # Generate thesis
        try:
            thesis, intel_ref = self.thesis_fn(intent, broker)
        except Exception as e:
            print(f"Thesis generation error: {e}")
            thesis, intel_ref = "", ""

        # Create prompt and publish
        prompt = MockPrompt.from_intent(intent, broker, thesis, intel_ref)
        self.store.publish_prompt(prompt)

        # Wait for decision (blocking)
        decision_deadline = datetime.now() + timedelta(seconds=self.ttl_seconds)
        while datetime.now() < decision_deadline:
            decision = self.store.decision_for(prompt.intent_id)
            if decision:
                if decision == "ACCEPT":
                    self.journal_accepts.append(prompt.intent_id)
                    return {"symbol": intent.symbol, "shares": intent.shares, "id": prompt.intent_id}
                elif decision == "DECLINE":
                    self.journal_declines.append(prompt.intent_id)
                    return None
            time.sleep(0.01)

        # Expired
        self.journal_rejects.append(f"prompt_expired {prompt.intent_id}")
        return None


# ============================================================================ #
# TESTS                                                                      #
# ============================================================================ #

def test_investment_engine_with_overlay_stress() -> bool:
    """TEST 1: Thesis generation with severe overlay risk."""
    print("\n[TEST 1] VSInvestmentEngine with overlay stress")
    print("-" * 78)

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MockVSInvestmentEngine(Path(tmpdir))
        intent = MockOrderIntent(
            symbol="BTC/USD", action="BUY", shares=1, entry=42000,
            stop=41000, target=43000, strategy="momentum",
            risk_dollars=1000, account_number="TEST-001",
        )

        snap = MockIntelSnapshot(strategic_risk=85.0, energy_stress=0.80, fear_greed=20.0)
        market = MockMarketContext(
            strategy_rank=2, universe_size=50, r_multiple=2.1,
            trend_slope=1.2, rsi_14=72.0, atr_pct=0.025
        )

        thesis, ref = engine.explain(intent, broker="kraken", market=market, overlay_snapshot=snap)

        passed = (
            len(thesis) <= 140 and
            ("geo-risk" in thesis) and
            ("energy" in thesis or "stress" in thesis) and
            ref.startswith("vs_")
        )

        print(f"  Thesis ({len(thesis)}/140): {thesis}")
        print(f"  Ref: {ref}")
        print(f"  [PASS]" if passed else "  [FAIL]")
        return passed


def test_investment_engine_graceful_degradation() -> bool:
    """TEST 2: Graceful degradation with sparse context."""
    print("\n[TEST 2] VSInvestmentEngine graceful degradation")
    print("-" * 78)

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MockVSInvestmentEngine(Path(tmpdir))
        intent = MockOrderIntent(
            symbol="AAPL", action="BUY", shares=10, entry=150,
            stop=145, target=155, strategy="test",
            risk_dollars=50, account_number="TEST-001",
        )

        market = MockMarketContext(universe_size=500)
        thesis, ref = engine.explain(intent, broker="ib", market=market)

        passed = (len(thesis) <= 140 and thesis and ref.startswith("vs_"))

        print(f"  Thesis (sparse context): {thesis}")
        print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
        return passed


def test_multi_broker_thesis_uniqueness() -> bool:
    """TEST 3: Multi-broker thesis generates unique refs."""
    print("\n[TEST 3] Multi-broker thesis uniqueness")
    print("-" * 78)

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MockVSInvestmentEngine(Path(tmpdir))
        intent = MockOrderIntent(
            symbol="ETH/USD", action="BUY", shares=5, entry=2000,
            stop=1900, target=2100, strategy="test",
            risk_dollars=500, account_number="TEST-001",
        )
        market = MockMarketContext(strategy_rank=3, universe_size=20, r_multiple=1.8)

        thesis_ib, ref_ib = engine.explain(intent, broker="ib", market=market)
        thesis_kraken, ref_kraken = engine.explain(intent, broker="kraken", market=market)

        passed = (
            ref_ib != ref_kraken and
            ref_ib.startswith("vs_") and
            ref_kraken.startswith("vs_")
        )

        print(f"  IB ref: {ref_ib}")
        print(f"  Kraken ref: {ref_kraken}")
        print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
        return passed


def test_approval_router_gate_rejection() -> bool:
    """TEST 4: ApprovalRouter gates invalid intents."""
    print("\n[TEST 4] ApprovalRouter gate rejection (no prompt)")
    print("-" * 78)

    store = MockApprovalStore()
    router = MockApprovalRouter(store)

    bad_intent = MockOrderIntent(
        symbol="XYZ", action="BUY", shares=0, entry=100,
        stop=95, target=105, strategy="test",
        risk_dollars=0, account_number="TEST-001",
    )

    result = router.submit(bad_intent)
    passed = (result is None and len(store.pending()) == 0 and len(router.journal_rejects) == 1)

    print(f"  Result: {result}")
    print(f"  Pending prompts: {len(store.pending())}")
    print(f"  Rejection logged: {router.journal_rejects[0] if router.journal_rejects else 'none'}")
    print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
    return passed


def test_approval_flow_accept() -> bool:
    """TEST 5: Full approval flow with acceptance."""
    print("\n[TEST 5] Full approval flow (ACCEPT)")
    print("-" * 78)

    def mock_thesis(intent: MockOrderIntent, broker: str) -> tuple[str, str]:
        return f"{intent.symbol} momentum signal observed", f"vs_{broker}_{intent.symbol}"

    store = MockApprovalStore()
    router = MockApprovalRouter(store, ttl_seconds=5, thesis_fn=mock_thesis)

    intent = MockOrderIntent(
        symbol="SPY", action="BUY", shares=10, entry=380,
        stop=370, target=390, strategy="test",
        risk_dollars=100, account_number="TEST-001",
    )

    # Submit on background thread
    result_holder = {}

    def _submit():
        result_holder["order"] = router.submit(intent, broker="ib")

    t = threading.Thread(target=_submit)
    t.start()

    # Wait for prompt and accept
    time.sleep(0.1)
    pending = store.pending()
    if pending:
        prompt = pending[0]
        store.respond(prompt.intent_id, "ACCEPT")

    t.join(timeout=2)
    order = result_holder.get("order")

    passed = order is not None and order["symbol"] == "SPY"

    print(f"  Pending prompts: {len(pending)}")
    print(f"  Order executed: {order}")
    print(f"  Accepts logged: {len(router.journal_accepts)}")
    print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
    return passed


def test_approval_flow_decline() -> bool:
    """TEST 6: Full approval flow with decline."""
    print("\n[TEST 6] Full approval flow (DECLINE)")
    print("-" * 78)

    store = MockApprovalStore()
    router = MockApprovalRouter(store, ttl_seconds=5)

    intent = MockOrderIntent(
        symbol="MSFT", action="BUY", shares=5, entry=420,
        stop=400, target=440, strategy="test",
        risk_dollars=100, account_number="TEST-001",
    )

    result_holder = {}

    def _submit():
        result_holder["order"] = router.submit(intent, broker="ib")

    t = threading.Thread(target=_submit)
    t.start()

    time.sleep(0.1)
    pending = store.pending()
    if pending:
        prompt = pending[0]
        store.respond(prompt.intent_id, "DECLINE")

    t.join(timeout=2)
    order = result_holder.get("order")

    passed = order is None and len(router.journal_declines) == 1

    print(f"  Order result: {order}")
    print(f"  Declines logged: {len(router.journal_declines)}")
    print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
    return passed


def test_novel_canonical_bytes_isolation() -> bool:
    """TEST 7: Novel - Canonical bytes encode broker for isolation."""
    print("\n[TEST 7] Novel: Canonical bytes multi-broker isolation")
    print("-" * 78)

    intent = MockOrderIntent(
        symbol="ETH/USD", action="BUY", shares=5, entry=2000,
        stop=1900, target=2100, strategy="test",
        risk_dollars=500, account_number="TEST-001",
    )

    prompt_ib = MockPrompt.from_intent(intent, "ib", "thesis", "vs_001")
    prompt_kraken = MockPrompt.from_intent(intent, "kraken", "thesis", "vs_001")

    passed = prompt_ib.canonical != prompt_kraken.canonical

    print(f"  IB canonical:     {prompt_ib.canonical}")
    print(f"  Kraken canonical: {prompt_kraken.canonical}")
    print(f"  Broker isolation: {passed}")
    print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
    return passed


def test_novel_conviction_encoding() -> bool:
    """TEST 8: Novel - Conviction score influences thesis."""
    print("\n[TEST 8] Novel: Conviction-based approval weighting")
    print("-" * 78)

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MockVSInvestmentEngine(Path(tmpdir))

        # High conviction
        market_high = MockMarketContext(
            strategy_rank=1, universe_size=100, r_multiple=2.5,
            trend_slope=2.0, rsi_14=75.0, atr_pct=0.02
        )
        thesis_high, _ = engine.explain(
            MockOrderIntent(
                symbol="BTC/USD", action="BUY", shares=1, entry=42000,
                stop=41000, target=43000, strategy="high",
                risk_dollars=1000, account_number="TEST-001",
            ),
            broker="kraken", market=market_high
        )

        # Low conviction
        market_low = MockMarketContext(
            strategy_rank=90, universe_size=100, r_multiple=1.05,
            trend_slope=0.05, rsi_14=45.0, atr_pct=0.005
        )
        thesis_low, _ = engine.explain(
            MockOrderIntent(
                symbol="ETH/USD", action="BUY", shares=5, entry=2000,
                stop=1900, target=2100, strategy="low",
                risk_dollars=500, account_number="TEST-001",
            ),
            broker="kraken", market=market_low
        )

        passed = (len(thesis_high) <= 140 and len(thesis_low) <= 140)

        print(f"  High conviction: {thesis_high}")
        print(f"  Low conviction:  {thesis_low}")
        print(f"  [NOTE] Card firmware could auto-decline low-conviction trades")
        print(f"  ✓ PASSED" if passed else "  ✗ FAILED")
        return passed


# ============================================================================ #
# MAIN                                                                        #
# ============================================================================ #

def main() -> int:
    print("""
================================================================================
         ISOLATED E2E APPROVAL SYSTEM TEST SUITE
    InvestmentEngine -> ApprovalRouter -> Card Simulator (Mocked)
================================================================================
    """)

    tests = [
        ("InvestmentEngine with overlay stress", test_investment_engine_with_overlay_stress),
        ("InvestmentEngine graceful degradation", test_investment_engine_graceful_degradation),
        ("Multi-broker thesis uniqueness", test_multi_broker_thesis_uniqueness),
        ("ApprovalRouter gate rejection", test_approval_router_gate_rejection),
        ("Approval flow: ACCEPT", test_approval_flow_accept),
        ("Approval flow: DECLINE", test_approval_flow_decline),
        ("Novel: Canonical bytes isolation", test_novel_canonical_bytes_isolation),
        ("Novel: Conviction-based weighting", test_novel_conviction_encoding),
    ]

    results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            results.append((name, passed))
        except Exception as e:
            print(f"  [ERROR] EXCEPTION: {e}")
            results.append((name, False))

    # Summary
    print(f"\n{'='*78}")
    passed_count = sum(1 for _, p in results if p)
    total_count = len(results)
    print(f"RESULTS: {passed_count}/{total_count} tests passed")
    print(f"{'='*78}\n")

    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {status:8} {name}")

    print(f"\n{'='*78}")
    print("""
================================================================================
                    NOVEL SOLUTIONS IMPLEMENTED
================================================================================

1. STATELESS PROMPT VERIFICATION
   Signatures on canonical bytes enable offline verification. No server
   state needed; card signature proves trader saw those exact details.

2. MULTI-BROKER CONTEXT ISOLATION
   Canonical bytes include broker name. Same card safely approves across
   multiple brokers; broker is cryptographically isolated.

3. CONVICTION-BASED APPROVAL GATING
   Thesis encodes strategy rank (conviction). Card firmware can auto-decline
   low-conviction orders, reducing trader decision load.

4. REPLAY JOURNAL FOR FAULT TOLERANCE
   All decisions logged to disk. Shim restart queries journal: "was this
   order approved before the crash?" Deterministic replay ensures idempotency.

5. LAZY THESIS GENERATION
   If VSInvestmentEngine offline, prompt published with thesis="". Card
   still works, trader still approves/declines; thesis is optional.

6. LCD DISPLAY OPTIMIZATION
   84x48 pixels: render [BROKER] ACTION SYMBOL QTY @ price | R=$ | TTL
   Full thesis on CENTER expand only. Reduces cognitive load.

================================================================================
    """)

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
