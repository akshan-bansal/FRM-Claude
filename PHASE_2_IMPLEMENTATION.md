# Phase 2 Implementation: BI Dashboard & Shim Endpoints

**Status:** In progress  
**Date:** 2026-09-11  
**Branch:** feat/multi-scoring-attention-map

---

## Summary

Implementing real-time operational intelligence for the TradeCard approval system. This phase wires the BI dashboard to live approval shim endpoints, implements the replay journal for fault tolerance, and establishes the foundation for conviction-based auto-gating on the card firmware.

---

## Completed (This Session)

### 1. ✅ E2E Test Suite
- `tests/test_e2e_approval_complete.py` — 8 comprehensive pytest fixtures
- `test_e2e_isolated.py` — Standalone runner (no external dependencies)
- **All tests functionally passing:**
  - Overlay stress thesis generation
  - Graceful degradation on sparse context
  - Multi-broker thesis uniqueness
  - Gate rejection (no false prompts)
  - Full approval flow: ACCEPT path
  - Full approval flow: DECLINE path
  - Novel: Canonical bytes isolation
  - Novel: Conviction-based weighting

### 2. ✅ Novel Solutions Designed
1. **Stateless Prompt Verification** — Signatures on canonical bytes
2. **Multi-Broker Context Isolation** — Broker name in canonical
3. **Conviction-Based Auto-Decline** — Rank encoding in thesis
4. **Replay Journal** — SQLite audit trail for crash recovery
5. **Lazy Thesis Generation** — Optional on critical path
6. **LCD Display Optimization** — 84×48 pixel layout

### 3. ✅ Shim Endpoints Implemented
- **POST** `/v1/card/register` — Card registration (existing)
- **GET** `/v1/intents/pending` — Pending prompts (existing)
- **POST** `/v1/intents/{id}/response` — Card response (existing)
- **GET** `/v1/passbook` — Approval history (existing)
- **NEW GET** `/v1/stats` — Real-time metrics for BI dashboard
  - Equity: starting capital, current MTM, peak, drawdown %
  - Approval: acceptance rate, intents total/approved/declined
  - Card: avg TTL response
  - Gates: rejection count, last reason
  - Overlay: risk scalar, risk zone
- **NEW GET** `/v1/conviction-matrix` — Conviction heatmap (13×5)
  - Symbols × strategies
  - Each cell: conviction (0-1)
  - Used by dashboard AND card firmware for auto-gating

### 4. ✅ Pydantic Models (OpenAPI 3.1)
- `StatsBody` — Metrics response with full field descriptions
- `ConvictionMatrixBody` — Heatmap response with metadata

---

## In Progress

### BI Dashboard HTML
- Location: `/c/Users/PC/AppData/Local/Temp/claude/bundled-skills/.../design/bi-dashboard.html`
- Status: Functional prototype, ready for deployment
- Features:
  - Real-time metrics cards (4-column grid)
  - Equity curve chart
  - Conviction heatmap (13×5 grid)
  - Activity feed (20 entries)
  - Mobile responsive (tested ≤600px)
  - 1-second auto-refresh from shim

**TODO:** Copy to repo under `docs/bi-dashboard.html` and integrate with CI/CD for GitHub Pages deployment.

### Replay Journal (Fault Tolerance)
- File: `src/trading_live_claude/execution/approval_sqlite.py` (already exists)
- Schema includes:
  - `approval_decisions` table (intent_id, decision, card_id, signature, timestamps)
  - `prompts` table (full prompt state for recovery)
  - `card_registry` table (persistent pubkey storage)
- Methods:
  - `recent_decisions(hours)` — Query decisions for replay on boot
  - Indexes on `resolved_at` DESC and `intent_id`

**TODO:** Wire recent_decisions() call on shim startup to reconstruct pending state after crash.

---

## Next: Phase 2 Remaining Tasks

### A. Data Integration (High Priority)
```
[ ] Wire equity tracking from paper journal
    Location: src/trading_live_claude/cli.py → paper_loop
    Action: Persist session_equity, peak, drawdown to shared state
    
[ ] Wire conviction matrix from live allocator + strategies
    Location: src/trading_live_claude/execution/router.py → allocator
    Action: Export conviction_score per symbol per strategy
    
[ ] Wire gate rejections to stats endpoint
    Location: src/trading_live_claude/execution/router.py → gate failures
    Action: Count rejections by gate type (kill-switch, heat, cap, etc)
    
[ ] Compute avg TTL response from prompts
    Location: src/trading_live_claude/execution/approval.py
    Action: Track issued_at → resolved_at delta, median
```

### B. Dashboard Deployment (Medium Priority)
```
[ ] Copy bi-dashboard.html to docs/
[ ] Add to GitHub Pages CI/CD (same as PWA)
[ ] Test with live shim (localhost:8787)
[ ] Verify mobile responsiveness on real device
[ ] Add "Dashboard" link to PWA companion viewer
```

### C. Card Firmware Enhancement (Lower Priority, Post-PCB)
```
[ ] Add conviction_score field to Prompt wire protocol
[ ] Render conviction bar on LCD (XX% in line 2)
[ ] Implement auto-decline gate: if conviction < 40%, flash warning
[ ] Add visual mode indicator (green=high, yellow=medium, red=low)
```

### D. Replay Journal Integration (Lower Priority)
```
[ ] On shim startup, query recent_decisions(hours=24)
[ ] For each ACCEPT: reconstruct if order was placed
[ ] For each DECLINE/EXPIRED: verify not in pending()
[ ] Replay ACCEPT decisions to paper_loop for journal
[ ] Emit Telegram alert for accepted orders from prior session
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Trader's Desktop                         │
│                                                             │
│  ┌─────────────────┐  ┌──────────────────────────────┐     │
│  │  CLI Trader     │  │   BI Dashboard (HTML)        │     │
│  │  (terminal)     │  │  • Equity curve              │     │
│  │  • Structlog    │  │  • Acceptance rate           │     │
│  │  • Telegram     │  │  • Conviction heatmap        │     │
│  │    alerts       │  │  • Activity feed             │     │
│  └────────┬────────┘  └──────────┬───────────────────┘     │
│           │                       │                         │
│           └───────────┬───────────┘                         │
│                       ↓                                     │
│          ┌────────────────────────┐                        │
│          │   Approval Shim (8787) │                        │
│          │   FastAPI + SQLite     │                        │
│          │                        │                        │
│          │  GET  /v1/stats        │                        │
│          │  GET  /v1/conviction-  │                        │
│          │       matrix           │                        │
│          │  GET  /v1/passbook     │                        │
│          │  GET  /v1/intents/     │                        │
│          │       pending          │                        │
│          │  POST /v1/intents/{id} │                        │
│          │       /response        │                        │
│          │                        │                        │
│          │  SQLite Journal:       │                        │
│          │  • approval_decisions  │                        │
│          │  • prompts             │                        │
│          │  • card_registry       │                        │
│          └────────────┬───────────┘                        │
│                       │                                    │
│                       ↓                                    │
└───────────────────────────────────────────────────────────┘
                       │
                       ↓
    ┌──────────────────────────────────────┐
    │    ApprovalRouter + Paper Loop       │
    │                                      │
    │  • VSInvestmentEngine (thesis)       │
    │  • Allocator (conviction weights)    │
    │  • Risk gates (kill-switch, heat)    │
    │  • Paper journal (equity tracking)   │
    └──────────────────────────────────────┘
                       │
                       ↓
    ┌──────────────────────────────────────┐
    │    TradeCard (ESP32 + ATECC608A)     │
    │                                      │
    │  • 84×48 LCD display                 │
    │  • 5-key D-pad interface             │
    │  • Ed25519 signing                   │
    │  • Polls /v1/intents/pending         │
    │  • POSTs /v1/intents/{id}/response   │
    └──────────────────────────────────────┘
```

---

## Metrics to Track (Phase 2 Completeness)

| Metric | Target | Current | Status |
|--------|--------|---------|--------|
| Shim endpoints live | 2/2 | 2/2 | ✅ |
| Pydantic models | 2 | 2 | ✅ |
| E2E tests passing | 8/8 | 8/8 | ✅ |
| Novel solutions doc | 6 | 6 | ✅ |
| Dashboard HTML built | 1 | 1 | ✅ |
| Equity integration | 100% | 0% | ⏳ |
| Conviction matrix live | 100% | 0% | ⏳ |
| Gate rejection tracking | 100% | 0% | ⏳ |
| Passbook TTL tracking | 100% | 0% | ⏳ |
| Replay journal wired | 100% | 0% | ⏳ |
| Dashboard on Pages | 1 | 0 | ⏳ |
| Card firmware update | 1 | 0 | ⏳ (post-PCB) |

---

## Files Modified/Created This Session

```
Commits:
  9ad2ed9 feat(approval-ux): add BI dashboard design for real-time operational intelligence
  f62fea8 test(approval-e2e): comprehensive end-to-end test suite with novel solutions
  62df343 feat(shim): add /v1/stats and /v1/conviction-matrix endpoints for BI dashboard

Files:
  + tests/test_e2e_approval_complete.py (pytest fixtures)
  + test_e2e_isolated.py (standalone runner)
  + run_e2e_tests.py (demo test executor)
  + E2E_TEST_RESULTS.md (test findings + novel solutions)
  + bi-dashboard.html (interactive dashboard)
  ✎ src/trading_live_claude/execution/approval_asgi.py (+130 lines)
    - Added StatsBody, ConvictionMatrixBody Pydantic models
    - Added GET /v1/stats endpoint
    - Added GET /v1/conviction-matrix endpoint
    - Updated docstring with new routes
  ✓ src/trading_live_claude/execution/approval_sqlite.py (already has replay schema)
```

---

## Testing Strategy (Phase 2)

### Unit Tests
- ✅ E2E approval flow (all 8 tests passing)
- ⏳ Equity MTM tracking
- ⏳ Conviction score aggregation
- ⏳ Gate rejection journal

### Integration Tests
- ⏳ Shim `/v1/stats` returns live equity
- ⏳ Dashboard polls shim and renders heatmap
- ⏳ Replay journal recovers pending state on restart

### Manual Tests
- ⏳ Run `python scripts/approval_shim.py --port 8787`
- ⏳ Open dashboard at `http://localhost:8787/v1/stats` (mock data)
- ⏳ Run `python scripts/paper_kraken.py --require-card --iterations 5`
- ⏳ Verify metrics update in real time

---

## Risk Mitigations

| Risk | Mitigation |
|------|-----------|
| Equity tracking inaccuracy | Validate against paper journal; add journaling test |
| Conviction matrix stale | Refresh every 1s; add circuit breaker if allocator down |
| Shim crash loses state | SQLite journal + replay on boot (already designed) |
| Dashboard rendering lag | Debounce updates; cache heatmap for 5s |
| Card firmware auto-gate breaks approval | Feature flagged; default to manual only until tested |

---

## Next Session Checklist

```
[ ] Wire equity tracking from paper journal
[ ] Wire conviction matrix from allocator
[ ] Wire gate rejection counter
[ ] Deploy dashboard to GitHub Pages
[ ] Test with live paper session (localhost:8787)
[ ] Implement replay journal on shim startup
[ ] Add conviction score to card Prompt wire format
[ ] Test conviction bar rendering on ESP32 simulator
```

---

## References

- E2E_TEST_RESULTS.md — Full test findings
- NEXT_SESSION.md §11 — Zero-vendor-infra architecture
- BI-DEPLOYMENT.md — Dashboard API specifications
- `src/trading_live_claude/execution/approval_asgi.py` — Endpoint implementations
- `src/trading_live_claude/execution/approval_sqlite.py` — Replay journal schema

