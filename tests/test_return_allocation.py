"""Owner-only allocation of returns (return_allocation.py, scripts/allocate.py).

Keys are generated per test and nothing is real. What is checked: the split is exact to the cent, a
request authorizes only with the owner's signature over its exact bytes, every other path is refused
AND recorded, the journal exposes tampering and forgery offline, and nothing that trades touches it.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.return_allocation import (
    AllocationGate,
    AllocationRefused,
    AllocationRequest,
    AuthorizedAllocation,
    build_plan,
    canonical_bytes,
    fingerprint,
    generate_owner_key,
    sign_request,
    verify_journal,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "trading_live_claude"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
PRIV, PUB = generate_owner_key()
OTHER_PRIV, OTHER_PUB = generate_owner_key()


def req(**kw) -> AllocationRequest:
    base = dict(request_id="alloc-1", period="2026-09", currency="CAD", total_cents=100_00,
                rule="pro_rata", recipients=(("alice", 300), ("bob", 200)),
                expires_at=(NOW + timedelta(minutes=5)).isoformat(), nonce="n0nce")
    base.update(kw)
    return AllocationRequest(**base)


def gate(tmp_path: Path, **kw) -> AllocationGate:
    return AllocationGate({"owner-1": PUB}, journal_path=tmp_path / "alloc.jsonl", max_ttl_s=900,
                          clock=lambda: NOW, **kw)


def journal(tmp_path: Path) -> list[dict]:
    p = tmp_path / "alloc.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ---- the arithmetic --------------------------------------------------------------------------

def test_a_split_is_exact_and_deterministic() -> None:
    p = build_plan(req(total_cents=100, recipients=(("a", 1), ("b", 1), ("c", 1))))
    assert p.lines == (("a", 34), ("b", 33), ("c", 33))                      # the odd cent goes to the first id
    assert build_plan(req(total_cents=-100, recipients=(("a", 1), ("b", 1), ("c", 1)))).lines == \
        (("a", -34), ("b", -33), ("c", -33))                                 # a loss splits the same way
    assert dict(build_plan(req(recipients=(("a", 0), ("b", 5)))).lines) == {"a": 0, "b": 100_00}


def test_every_random_split_sums_to_the_cent_and_is_within_one_cent_of_exact() -> None:
    rng = np.random.default_rng(0)
    for _ in range(3000):
        n = int(rng.integers(1, 9))
        weights = [int(w) for w in rng.integers(0, 10_000, size=n)]
        if sum(weights) == 0:
            weights[0] = 1
        total = int(rng.integers(1, 10**9)) * (1 if rng.random() < 0.8 else -1)
        r = req(total_cents=total, recipients=tuple((f"m{i}", w) for i, w in enumerate(weights)))
        lines = build_plan(r).lines
        assert sum(c for _, c in lines) == total
        for (_, c), w in zip(lines, weights, strict=True):
            assert abs(c - total * w / sum(weights)) < 1.0 + 1e-6


def test_explicit_amounts_must_add_up_and_pass_through() -> None:
    assert build_plan(req(rule="explicit", total_cents=70, recipients=(("a", 30), ("b", 40)))).lines == \
        (("a", 30), ("b", 40))
    with pytest.raises(ValueError, match="sum to"):
        req(rule="explicit", total_cents=70, recipients=(("a", 30), ("b", 41)))


@pytest.mark.parametrize("bad", [
    dict(total_cents=0), dict(total_cents=1.5), dict(total_cents=True), dict(currency="cad"),
    dict(request_id="a|b"), dict(period="x;y"), dict(nonce="a=b"), dict(rule="equal"),
    dict(recipients=()), dict(recipients=(("a", 1), ("a", 2))), dict(recipients=(("a", -1), ("b", 3))),
    dict(recipients=(("a", 0), ("b", 0))), dict(recipients=(("a b", 1),)), dict(recipients=(("a", 1.5),)),
    dict(expires_at="2026-10-04T12:05:00"), dict(expires_at="2026-10-04T08:05:00-04:00"),
    dict(expires_at="soon"),
])
def test_a_malformed_request_cannot_be_constructed(bad: dict) -> None:
    with pytest.raises(ValueError):
        req(**bad)


def test_the_signed_bytes_move_with_every_field_and_not_with_member_order() -> None:
    base = canonical_bytes(req())
    assert base.startswith(b"alloc/1|")
    for change in (dict(request_id="alloc-2"), dict(period="2026-10"), dict(currency="USD"),
                   dict(total_cents=100_01), dict(nonce="other"), dict(rule="explicit", total_cents=500,
                                                                         recipients=(("alice", 300), ("bob", 200))),
                   dict(recipients=(("alice", 300), ("bob", 201))), dict(recipients=(("alice", 300), ("bobby", 200))),
                   dict(expires_at=(NOW + timedelta(minutes=6)).isoformat())):
        assert canonical_bytes(req(**change)) != base
    assert canonical_bytes(req(recipients=(("bob", 200), ("alice", 300)))) == base
    assert re.fullmatch(r"[0-9A-F]{4}\.\.\.[0-9A-F]{4}", fingerprint(base))


# ---- authorization ---------------------------------------------------------------------------

def test_the_owners_signature_authorizes_and_is_recorded(tmp_path: Path) -> None:
    r = req()
    auth = gate(tmp_path).authorize(r, "owner-1", sign_request(r, PRIV))
    assert isinstance(auth, AuthorizedAllocation) and auth.plan.lines == (("alice", 60_00), ("bob", 40_00))
    rows = journal(tmp_path)
    assert [x["event"] for x in rows] == ["authorized"] and rows[0]["fingerprint"] == auth.fingerprint
    assert verify_journal(tmp_path / "alloc.jsonl", {"owner-1": PUB}) == []


def test_someone_elses_signature_is_refused_and_the_attempt_is_recorded(tmp_path: Path) -> None:
    r = req()
    with pytest.raises(AllocationRefused, match="signature does not verify"):
        gate(tmp_path).authorize(r, "owner-1", sign_request(r, OTHER_PRIV))
    rows = journal(tmp_path)
    assert [x["event"] for x in rows] == ["refused"] and "signature" in rows[0]["reason"]
    assert "lines" not in rows[0]                                            # a refusal releases no plan


def test_changing_anything_after_signing_voids_the_signature(tmp_path: Path) -> None:
    r = req()
    sig = sign_request(r, PRIV)
    with pytest.raises(AllocationRefused, match="signature does not verify"):
        gate(tmp_path).authorize(req(recipients=(("alice", 100), ("bob", 200))), "owner-1", sig)
    with pytest.raises(AllocationRefused, match="signature does not verify"):
        gate(tmp_path).authorize(req(total_cents=999_99), "owner-1", sig)


def test_an_unknown_signer_an_expired_request_and_a_far_future_expiry_are_refused(tmp_path: Path) -> None:
    g = gate(tmp_path)
    r = req()
    with pytest.raises(AllocationRefused, match="not an owner key"):
        g.authorize(r, "owner-2", sign_request(r, PRIV))
    old = req(request_id="alloc-old", expires_at=(NOW - timedelta(seconds=1)).isoformat())
    with pytest.raises(AllocationRefused, match="expired"):
        g.authorize(old, "owner-1", sign_request(old, PRIV))
    far = req(request_id="alloc-far", expires_at=(NOW + timedelta(days=30)).isoformat())
    with pytest.raises(AllocationRefused, match="away"):
        g.authorize(far, "owner-1", sign_request(far, PRIV))
    assert [x["event"] for x in journal(tmp_path)] == ["refused", "refused", "refused"]


def test_a_request_id_authorizes_once_so_a_captured_signature_cannot_be_replayed(tmp_path: Path) -> None:
    g = gate(tmp_path)
    r = req()
    sig = sign_request(r, PRIV)
    g.authorize(r, "owner-1", sig)
    with pytest.raises(AllocationRefused, match="already authorized"):
        g.authorize(r, "owner-1", sig)
    assert [x["event"] for x in journal(tmp_path)] == ["authorized", "refused"]


def test_an_authorization_cannot_be_constructed_by_hand(tmp_path: Path) -> None:
    r = req()
    with pytest.raises(PermissionError):
        AuthorizedAllocation(r, build_plan(r), "owner-1", "AAAA...BBBB", NOW.isoformat())


def test_the_gate_fails_closed_when_it_cannot_record(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    g = AllocationGate({"owner-1": PUB}, journal_path=blocker / "alloc.jsonl", max_ttl_s=900, clock=lambda: NOW)
    r = req()
    with pytest.raises(AllocationRefused, match="nothing was authorized"):
        g.authorize(r, "owner-1", sign_request(r, PRIV))


def test_a_gate_needs_owner_keys_and_a_positive_ttl(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no owner keys"):
        AllocationGate({}, journal_path=tmp_path / "j", max_ttl_s=900)
    with pytest.raises(ValueError, match="max_ttl_s"):
        AllocationGate({"owner-1": PUB}, journal_path=tmp_path / "j", max_ttl_s=0)
    with pytest.raises(TypeError):
        AllocationGate({"owner-1": PUB}, journal_path=tmp_path / "j")        # type: ignore[call-arg]


# ---- the journal exposes tampering and forgery ----------------------------------------------

def _two_authorizations(tmp_path: Path) -> Path:
    g = gate(tmp_path)
    for rid in ("alloc-1", "alloc-2"):
        r = req(request_id=rid)
        g.authorize(r, "owner-1", sign_request(r, PRIV))
    return tmp_path / "alloc.jsonl"


def test_an_edited_row_is_found(tmp_path: Path) -> None:
    p = _two_authorizations(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("6000", "9000", 1), encoding="utf-8")
    assert any("altered" in x for x in verify_journal(p, {"owner-1": PUB}))


def test_a_deleted_row_is_found(tmp_path: Path) -> None:
    p = _two_authorizations(tmp_path)
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text(lines[1] + "\n", encoding="utf-8")
    assert any("sequence" in x or "chain" in x for x in verify_journal(p, {"owner-1": PUB}))


def test_a_row_signed_by_someone_else_is_found_even_if_the_chain_is_rebuilt(tmp_path: Path) -> None:
    """The forger controls the file, so they can recompute every hash; they cannot sign as the owner."""
    from trading_live_claude.return_allocation import _row_hash
    p = _two_authorizations(tmp_path)
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    forged = req(request_id="alloc-forged", recipients=(("mallory", 1),))
    row = {"seq": 2, "prev_hash": rows[-1]["row_hash"], "event": "authorized", "ts": NOW.isoformat(),
           "request_id": "alloc-forged", "key_id": "owner-1", "fingerprint": fingerprint(canonical_bytes(forged)),
           "canonical": canonical_bytes(forged).decode(),
           "signature": base64.b64encode(sign_request(forged, OTHER_PRIV)).decode(),
           "request": forged.to_json(), "lines": [["mallory", 100_00]]}
    row["row_hash"] = _row_hash(row)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    problems = verify_journal(p, {"owner-1": PUB})
    assert len(problems) == 1 and "NOT signed by the owner" in problems[0]


def test_a_row_whose_lines_do_not_match_its_request_is_found(tmp_path: Path) -> None:
    from trading_live_claude.return_allocation import _row_hash
    p = _two_authorizations(tmp_path)
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    rows[1]["lines"] = [["alice", 99_00], ["bob", 1_00]]
    rows[1]["row_hash"] = _row_hash(rows[1])
    p.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
    assert any("differ from the plan" in x for x in verify_journal(p, {"owner-1": PUB}))


# ---- the command line ------------------------------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location(
        "allocate_script", Path(__file__).resolve().parent.parent / "scripts" / "allocate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["allocate_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_whole_flow_through_the_command_line(tmp_path: Path, monkeypatch, capsys) -> None:
    s = _script()
    priv, pub = tmp_path / "k" / "owner.key.pem", tmp_path / "owner.pub.pem"
    assert s.main(["keygen", "--id", "owner-1", "--private", str(priv), "--public", str(pub)]) == 0
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        s.main(["keygen", "--id", "owner-1", "--private", str(priv), "--public", str(pub)])
    reqf, sigf, jr = tmp_path / "req.json", tmp_path / "req.sig", tmp_path / "alloc.jsonl"
    assert s.main(["prepare", "--out", str(reqf), "--period", "2026-09", "--currency", "CAD",
                   "--total", "1234.56", "--member", "alice=300", "--member", "bob=200"]) == 0
    out = capsys.readouterr().out
    assert "fingerprint" in out and "Nothing is authorized" in out and "1,234.56" in out
    monkeypatch.setattr("builtins.input", lambda _="": "no")
    assert s.main(["sign", str(reqf), "--private", str(priv), "--out", str(sigf)]) == 1 and not sigf.exists()
    monkeypatch.setattr("builtins.input", lambda _="": "YES")
    assert s.main(["sign", str(reqf), "--private", str(priv), "--out", str(sigf)]) == 0
    common = ["--owner", f"owner-1={pub}", "--journal", str(jr)]
    assert s.main(["apply", str(reqf), str(sigf), "--signer", "owner-1", *common]) == 0
    assert "No money was moved" in capsys.readouterr().out
    assert s.main(["apply", str(reqf), str(sigf), "--signer", "owner-1", *common]) == 2     # replay
    assert "already authorized" in capsys.readouterr().err
    assert s.main(["verify", *common]) == 0 and "journal verifies" in capsys.readouterr().out


def test_amounts_with_fractions_of_a_cent_are_rejected_at_the_command_line(tmp_path: Path) -> None:
    s = _script()
    with pytest.raises(SystemExit, match="fractions of a cent"):
        s.to_cents("10.005")
    assert s.to_cents("10.50") == 1050 and s.to_cents("-0.01") == -1 and s.money(-123456) == "-1,234.56"


# ---- it is not part of anything that trades ---------------------------------------------------

def test_allocation_imports_nothing_that_trades_and_nothing_that_trades_imports_it() -> None:
    src = (SRC / "return_allocation.py").read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
    forbidden = ("execution", "risk", "brokers", "strategies", "monitor", "daemon", "sim", "intel")
    assert not [ln for ln in imports if any(re.search(rf"\b{w}\b", ln) for w in forbidden)]
    import ast
    offenders = []
    for f in SRC.rglob("*.py"):
        if f.name == "return_allocation.py":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8", errors="replace"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module or "", *(a.name for a in node.names)] if isinstance(node, ast.ImportFrom) else [])
            if any("return_allocation" in n for n in names):
                offenders.append(str(f.relative_to(SRC)))
    assert offenders == []
