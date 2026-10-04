"""Owner-only allocation of returns. Computes and records a plan; moves no money.

    python scripts/allocate.py keygen --id owner-1 --private <where only you keep it>.pem --public config/owner-1.pub.pem
    python scripts/allocate.py prepare --out req.json --period 2026-09 --currency CAD --total 1234.56 \\
        --rule pro_rata --member alice=300 --member bob=200
    python scripts/allocate.py sign req.json --private <key>.pem --out req.sig      (asks you to type YES)
    python scripts/allocate.py apply req.json req.sig --signer owner-1 --owner owner-1=config/owner-1.pub.pem
    python scripts/allocate.py verify --owner owner-1=config/owner-1.pub.pem

``prepare`` and ``verify`` change nothing that matters. ``sign`` needs your private key and an
interactive YES, so a process without a terminal cannot sign. ``apply`` records the plan only if the
signature verifies; every attempt, refused ones included, goes in the hash-chained journal
``state/allocations.jsonl``. See ``trading_live_claude/return_allocation.py`` for what this does and does not
protect against.
"""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_live_claude.return_allocation import (
    AllocationGate,
    AllocationRefused,
    AllocationRequest,
    build_plan,
    canonical_bytes,
    fingerprint,
    generate_owner_key,
    sign_request,
    verify_journal,
)

JOURNAL = Path("state/allocations.jsonl")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def to_cents(text: str) -> int:
    try:
        d = Decimal(text)
    except InvalidOperation as e:
        raise SystemExit(f"not an amount: {text!r}") from e
    cents = d * 100
    if cents != cents.to_integral_value():
        raise SystemExit(f"{text!r} has fractions of a cent; give whole cents")
    return int(cents)


def owners(pairs: list[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in pairs:
        key_id, _, path = p.partition("=")
        if not key_id or not path:
            raise SystemExit(f"--owner wants KEYID=PATH, got {p!r}")
        out[key_id] = Path(path).read_bytes()
    return out


def show(req: AllocationRequest) -> None:
    plan = build_plan(req)
    print(f"request {req.request_id}  period {req.period}  {req.currency} {money(req.total_cents)}  ({req.rule})")
    for member, cents in plan.lines:
        print(f"  {member:<24} {money(cents):>16}")
    print(f"  expires {req.expires_at}")
    print(f"  fingerprint {fingerprint(canonical_bytes(req))}   (compare this at every step)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--id", required=True)
    k.add_argument("--private", type=Path, required=True)
    k.add_argument("--public", type=Path, required=True)
    pr = sub.add_parser("prepare")
    pr.add_argument("--out", type=Path, required=True)
    pr.add_argument("--period", required=True)
    pr.add_argument("--currency", required=True)
    pr.add_argument("--total", required=True, help="the return to split, e.g. 1234.56 (negative for a loss)")
    pr.add_argument("--rule", choices=("pro_rata", "explicit"), default="pro_rata")
    pr.add_argument("--member", action="append", required=True, help="ID=WEIGHT (pro_rata) or ID=AMOUNT (explicit)")
    pr.add_argument("--ttl", type=int, default=600, help="seconds the request stays signable")
    sg = sub.add_parser("sign")
    sg.add_argument("request", type=Path)
    sg.add_argument("--private", type=Path, required=True)
    sg.add_argument("--out", type=Path, required=True)
    ap_ = sub.add_parser("apply")
    ap_.add_argument("request", type=Path)
    ap_.add_argument("signature", type=Path)
    ap_.add_argument("--signer", required=True)
    ap_.add_argument("--owner", action="append", required=True)
    ap_.add_argument("--journal", type=Path, default=JOURNAL)
    ap_.add_argument("--max-ttl", type=float, default=900.0)
    vf = sub.add_parser("verify")
    vf.add_argument("--owner", action="append", required=True)
    vf.add_argument("--journal", type=Path, default=JOURNAL)
    a = ap.parse_args(argv)

    if a.cmd == "keygen":
        for path in (a.private, a.public):
            if path.exists():
                raise SystemExit(f"{path} already exists; refusing to overwrite a key")
        private, public = generate_owner_key()
        a.private.parent.mkdir(parents=True, exist_ok=True)
        a.public.parent.mkdir(parents=True, exist_ok=True)
        a.private.write_bytes(private)
        a.public.write_bytes(public)
        print(f"key {a.id}: private -> {a.private}  (keep it off the repo and off synced folders)")
        print(f"            public  -> {a.public}   (this one is safe to share and to register with --owner)")
        return 0

    if a.cmd == "prepare":
        recipients = []
        for m in a.member:
            name, _, value = m.partition("=")
            recipients.append((name, to_cents(value) if a.rule == "explicit" else int(value)))
        expires = (datetime.now(UTC) + timedelta(seconds=a.ttl)).isoformat()
        req = AllocationRequest(request_id="alloc-" + secrets.token_hex(6), period=a.period,
                                currency=a.currency, total_cents=to_cents(a.total), rule=a.rule,
                                recipients=tuple(recipients), expires_at=expires, nonce=secrets.token_hex(8))
        a.out.write_text(json.dumps(req.to_json(), indent=2), encoding="utf-8")
        show(req)
        print(f"written to {a.out}. Nothing is authorized until you sign it.")
        return 0

    if a.cmd == "sign":
        req = AllocationRequest.from_json(json.loads(a.request.read_text(encoding="utf-8")))
        show(req)
        if input("Sign exactly this allocation? Type YES: ").strip() != "YES":
            print("not signed")
            return 1
        a.out.write_text(base64.b64encode(sign_request(req, a.private.read_bytes())).decode("ascii"),
                         encoding="utf-8")
        print(f"signed -> {a.out}")
        return 0

    if a.cmd == "apply":
        req = AllocationRequest.from_json(json.loads(a.request.read_text(encoding="utf-8")))
        gate = AllocationGate(owners(a.owner), journal_path=a.journal, max_ttl_s=a.max_ttl)
        try:
            auth = gate.authorize(req, a.signer, base64.b64decode(a.signature.read_text(encoding="utf-8")))
        except AllocationRefused as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 2
        show(auth.request)
        print(f"authorized by {auth.key_id} and recorded in {a.journal}. No money was moved.")
        return 0

    problems = verify_journal(a.journal, owners(a.owner))
    for p in problems:
        print("PROBLEM:", p)
    print("journal verifies" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
