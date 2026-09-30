"""Card-signed baskets: which symbols each venue may open new positions in.

    python scripts/basket.py status
    python scripts/basket.py sign --venue kraken --from-seed
    python scripts/basket.py sign --venue qt --symbols XIC.TO,VOO --analysis reports/qt_analysis.json
    python scripts/basket.py withdraw --venue ib

Signing shows the basket and its fingerprint the way a card screen would, then signs with the
SIMULATED card's key (state/card_sim/card-sim-001.pem). Physical card firmware cannot sign baskets
yet: it reads only the nine-field trade canonical and would refuse this one. The signature is
verified against the card paired in state/approval.db before anything is written, so a key the
shim does not know is refused here instead of being stored as an approval that never verifies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_card_sim import load_or_create_key

from trading_live_claude.execution.basket import (
    BasketBook,
    append_row,
    canonical_basket,
    fingerprint,
    ib_refusal,
    new_row,
    normalize,
    pending_proposals,
    registry_verifier,
    sign_proposal,
)

STATE = Path("state")
SEED = Path("config/basket_seed.json")
VENUES = ("kraken", "qt", "ib")
CARD_ID = "card-sim-001"
KEY_PATH = STATE / "card_sim" / f"{CARD_ID}.pem"


def _status() -> int:
    book = BasketBook(STATE / "baskets.jsonl", registry_verifier(STATE / "approval.db"))
    for v in VENUES:
        ap = book.approved(v)
        if ap is None:
            print(f"{v:<7} NO SIGNED BASKET  (entries rejected by any session started with the gate on)")
        else:
            print(f"{v:<7} {len(ap.symbols):>2} symbols  signed {ap.issued_at}  by {ap.card_id}  "
                  f"analysis {ap.analysis_hash or '-'}")
    if book.rejected_rows:
        print(f"{book.rejected_rows} row(s) in baskets.jsonl failed verification and count for nothing")
    return 0


def _pending() -> int:
    props = pending_proposals(STATE / "basket_proposals.jsonl")
    if not props:
        print("no pending basket proposals (propose one from the QuantPort.io BASKET tab)")
    for p in props:
        print(f"{p['id']}  {p['venue']:<7} {len(p['symbols']):>2} symbols  fingerprint {p['fingerprint']}  "
              f"proposed {p['issued_at']}")
    return 0


def _sign_proposal(pid: str, auto: bool) -> int:
    match = [p for p in pending_proposals(STATE / "basket_proposals.jsonl") if p["id"] == pid]
    if not match:
        print(f"no pending proposal {pid} (signed already, expired after 30 min, or never proposed)")
        return 2
    p = match[0]
    print(f"\nBASKET  venue={p['venue']}  {len(p['symbols'])} symbols  fingerprint {p['fingerprint']}")
    print("  " + ", ".join(p["symbols"]) if p["symbols"] else "  (empty: withdraws the venue's basket)")
    if not auto and input("ACCEPT on the sim card? [y/N] ").strip().lower() != "y":
        print("declined; nothing written")
        return 1
    key = load_or_create_key(KEY_PATH)
    row = sign_proposal(p, card_id=CARD_ID, sign=key.sign)
    if not registry_verifier(STATE / "approval.db")(CARD_ID, p["canonical"].encode("utf-8"),
                                                     __import__("base64").b64decode(row["signature"])):
        print(f"refused: {CARD_ID} is not paired in {STATE / 'approval.db'}")
        return 2
    append_row(row, STATE / "baskets.jsonl")
    print(f"signed and stored ({row['nonce']})")
    return 0


def _sign(venue: str, symbols: list[str], analysis: object, auto: bool) -> int:
    syms = normalize(symbols)
    if venue == "ib":
        bad = [r for r in (ib_refusal(s) for s in syms) if r]
        if bad:
            print("refused:\n  " + "\n  ".join(bad))
            return 2
    key = load_or_create_key(KEY_PATH)
    row = new_row(venue=venue, symbols=list(syms), analysis=analysis, card_id=CARD_ID, sign=key.sign)
    canon = canonical_basket(venue=venue, symbols=syms, analysis_digest=row["analysis_hash"],
                             issued_at=row["issued_at"], nonce=row["nonce"])
    print(f"\nBASKET  venue={venue}  {len(syms)} symbols  fingerprint {fingerprint(canon)}")
    print("  " + ", ".join(syms) if syms else "  (empty: withdraws the venue's basket)")
    if not auto and input("ACCEPT on the sim card? [y/N] ").strip().lower() != "y":
        print("declined; nothing written")
        return 1
    if not registry_verifier(STATE / "approval.db")(CARD_ID, canon, __import__("base64").b64decode(row["signature"])):
        print(f"refused: {CARD_ID} is not paired in {STATE / 'approval.db'} (start a shim and pair the sim card)")
        return 2
    append_row(row, STATE / "baskets.jsonl")
    print(f"signed and stored ({row['nonce']})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("pending", help="basket proposals queued from the panel, waiting for the card")
    sp = sub.add_parser("sign-proposal", help="sign a queued proposal with the simulated card")
    sp.add_argument("id")
    sp.add_argument("--auto", action="store_true", help="accept without the prompt (paper)")
    for name in ("sign", "withdraw"):
        p = sub.add_parser(name)
        p.add_argument("--venue", required=True, choices=VENUES)
        p.add_argument("--auto", action="store_true", help="accept without the prompt (paper)")
        if name == "sign":
            p.add_argument("--symbols", default="")
            p.add_argument("--from-seed", action="store_true", help=f"use {SEED}")
            p.add_argument("--analysis", type=Path, help="JSON file of the analysis shown; its hash is signed")
    a = ap.parse_args()
    if a.cmd == "status":
        return _status()
    if a.cmd == "pending":
        return _pending()
    if a.cmd == "sign-proposal":
        return _sign_proposal(a.id, a.auto)
    if a.cmd == "withdraw":
        return _sign(a.venue, [], None, a.auto)
    if a.from_seed == bool(a.symbols):
        ap.error("give exactly one of --symbols or --from-seed")
    syms = (json.loads(SEED.read_text(encoding="utf-8"))[a.venue] if a.from_seed
            else [s for s in a.symbols.split(",")])
    analysis = json.loads(a.analysis.read_text(encoding="utf-8")) if a.analysis else None
    return _sign(a.venue, syms, analysis, a.auto)


if __name__ == "__main__":
    sys.exit(main())
