"""Owner-only allocation of returns: a plan the owner signs, a journal that records it, nothing moves.

What this is: an adapter that turns "split this return among these members" into an exact plan in
integer cents, and records it ONLY if the exact bytes of that request carry a valid Ed25519
signature from a key the owner registered for this purpose. What it is not: it moves no money,
talks to no broker, and is not imported by anything that trades (``tests/test_return_allocation.py``
enforces both directions). Pooling other people's money is regulated activity; this module computes
and records a plan and stops there.

How "only the owner" is made true, and where it stops being true:

* **Authority is a signature, not a secret in the code.** :func:`build_plan` is arithmetic anyone can
  run. What the rest of the system should accept is an :class:`AuthorizedAllocation`, which only
  :meth:`AllocationGate.authorize` produces, and only after the signature over the request's canonical
  bytes verifies against an owner key. Without the private key a request cannot be made to verify.
* **What you sign is what you see.** The canonical bytes contain every field that matters (id, period,
  currency, total, rule, every member and value, expiry, nonce); ``fingerprint`` gives the short form
  to compare on screen. Change any field and the signature no longer verifies.
* **Not replayable, not long-lived.** A request id authorizes once; the expiry must be soon (the
  gate's ``max_ttl_s``); a stale or far-future request is refused.
* **Separate keys.** Owner keys are their own set, not the trade-approval cards. The cards' firmware
  signs only trade formats, and a trade signature cannot authorize an allocation: the canonical
  bytes start with ``alloc/1`` and no trade canonical does.
* **Everything is recorded, including refusals,** in a hash-chained journal; :func:`verify_journal`
  re-checks the chain, every signature and every plan offline.

The limit, stated plainly: the gate runs on the machine it protects. Someone who can edit this code or
the journal can call around it. What they cannot do is forge an authorization that verifies against the
owner's public key, so a bypass shows up in :func:`verify_journal` as an unsigned row or a broken chain.
Keep the private key off the repo and off this drive's synced folders.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GENESIS = "0" * 64
CANONICAL_PREFIX = "alloc/1"
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CUR = re.compile(r"^[A-Z]{3}$")
Rule = Literal["pro_rata", "explicit"]


class AllocationRefused(Exception):
    """The request was not authorized. The reason is in the message and in the journal."""


def _ident(name: str, value: object) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{name} must match [A-Za-z0-9_.:-]{{1,64}}, got {value!r}")
    return value


def _utc_text(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"expires_at is not an ISO-8601 time: {value!r}") from e
    if dt.tzinfo is None or dt.astimezone(UTC).isoformat() != value:
        raise ValueError(f"expires_at must be written in UTC as {dt.astimezone(UTC).isoformat()!r}, "
                         f"got {value!r}")
    return dt


# ---- the request and the plan ---------------------------------------------------------------

@dataclass(frozen=True)
class AllocationRequest:
    """What the owner is asking for. Money is integer cents; never a float."""

    request_id: str
    period: str                                   # a label, e.g. "2026-09"
    currency: str                                 # ISO 4217 code, e.g. "CAD"
    total_cents: int                              # the return to split; may be negative (a loss)
    rule: Rule                                    # "pro_rata" by integer weight, or "explicit" cents
    recipients: tuple[tuple[str, int], ...]       # (member id, weight or cents)
    expires_at: str                               # UTC ISO-8601, exactly as datetime.isoformat() writes it
    nonce: str

    def __post_init__(self) -> None:
        _ident("request_id", self.request_id)
        _ident("period", self.period)
        _ident("nonce", self.nonce)
        if not isinstance(self.currency, str) or not _CUR.match(self.currency):
            raise ValueError(f"currency must be three capital letters, got {self.currency!r}")
        for n, v in (("total_cents", self.total_cents), *(("value", x) for _, x in self.recipients)):
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"{n} must be an integer number of cents or weight, got {v!r}")
        if self.total_cents == 0:
            raise ValueError("total_cents is zero: nothing to allocate")
        if self.rule not in ("pro_rata", "explicit"):
            raise ValueError(f"rule must be 'pro_rata' or 'explicit', got {self.rule!r}")
        if not self.recipients:
            raise ValueError("no recipients")
        members = [_ident("member", m) for m, _ in self.recipients]
        if len(set(members)) != len(members):
            raise ValueError("a member appears twice")
        object.__setattr__(self, "recipients", tuple(sorted(self.recipients)))
        _utc_text(self.expires_at)
        values = [v for _, v in self.recipients]
        if self.rule == "pro_rata":
            if min(values) < 0 or sum(values) <= 0:
                raise ValueError("pro_rata weights must be >= 0 with a positive sum")
        elif sum(values) != self.total_cents:
            raise ValueError(f"explicit amounts sum to {sum(values)}, not {self.total_cents}")

    def to_json(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "period": self.period, "currency": self.currency,
                "total_cents": self.total_cents, "rule": self.rule,
                "recipients": [[m, v] for m, v in self.recipients],
                "expires_at": self.expires_at, "nonce": self.nonce}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> AllocationRequest:
        return cls(request_id=d["request_id"], period=d["period"], currency=d["currency"],
                   total_cents=d["total_cents"], rule=d["rule"],
                   recipients=tuple((str(m), v) for m, v in d["recipients"]),
                   expires_at=d["expires_at"], nonce=d["nonce"])


@dataclass(frozen=True)
class AllocationPlan:
    request_id: str
    currency: str
    total_cents: int
    lines: tuple[tuple[str, int], ...]            # (member id, cents); sums to total_cents exactly


def build_plan(req: AllocationRequest) -> AllocationPlan:
    """Exact split. ``pro_rata`` uses largest-remainder rounding (ties by member id), so the lines
    always sum to the total to the cent and the same request always gives the same plan."""
    if req.rule == "explicit":
        lines = req.recipients
    else:
        weight = sum(w for _, w in req.recipients)
        magnitude, sign = abs(req.total_cents), (1 if req.total_cents > 0 else -1)
        parts = [(m, magnitude * w // weight, magnitude * w % weight) for m, w in req.recipients]
        left = magnitude - sum(base for _, base, _ in parts)
        order = sorted(range(len(parts)), key=lambda i: (-parts[i][2], parts[i][0]))
        bump = set(order[:left])
        lines = tuple((m, sign * (base + (1 if i in bump else 0))) for i, (m, base, _) in enumerate(parts))
    plan = AllocationPlan(req.request_id, req.currency, req.total_cents, tuple(lines))
    if sum(c for _, c in plan.lines) != plan.total_cents:                      # pragma: no cover
        raise AssertionError("allocation does not sum to its total")
    return plan


def canonical_bytes(req: AllocationRequest) -> bytes:
    """The exact bytes the owner signs."""
    members = ";".join(f"{m}={v}" for m, v in req.recipients)
    return "|".join([CANONICAL_PREFIX, req.request_id, req.period, req.currency, str(req.total_cents),
                     req.rule, members, req.expires_at, req.nonce]).encode("utf-8")


def fingerprint(canonical: bytes, *, chars: int = 8) -> str:
    """Short form to compare on screen: ``7F3A...91C2``. Verification always uses the full signature."""
    digest = sha256(canonical).hexdigest().upper()
    half = chars // 2
    return f"{digest[:half]}...{digest[-half:]}"


# ---- keys -----------------------------------------------------------------------------------

def generate_owner_key() -> tuple[bytes, bytes]:
    """A new Ed25519 pair as (private PEM, public PEM). The private half is yours alone."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public


def sign_request(req: AllocationRequest, private_pem: bytes) -> bytes:
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("owner key must be Ed25519")
    return key.sign(canonical_bytes(req))


def _load_public(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("owner key must be Ed25519")
    return key


def _verifies(key: Ed25519PublicKey, canonical: bytes, signature: bytes) -> bool:
    try:
        key.verify(signature, canonical)
        return True
    except InvalidSignature:
        return False


# ---- the journal ----------------------------------------------------------------------------

def _row_hash(row: Mapping[str, Any]) -> str:
    body = {k: v for k, v in row.items() if k != "row_hash"}
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class _Journal:
    """Append-only, fsynced, hash-chained JSONL. A failed write raises: no record, no authorization."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def append(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rows = self.rows()
            row = {"seq": len(rows), "prev_hash": rows[-1]["row_hash"] if rows else GENESIS, **body}
            row["row_hash"] = _row_hash(row)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return row


_GATE_TOKEN = object()


@dataclass(frozen=True)
class AuthorizedAllocation:
    """A plan the owner signed. Only :meth:`AllocationGate.authorize` makes one (advisory in Python:
    the proof that matters is the signature on the journal row)."""

    request: AllocationRequest
    plan: AllocationPlan
    key_id: str
    fingerprint: str
    authorized_at: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _GATE_TOKEN:
            raise PermissionError("an AuthorizedAllocation can only be produced by AllocationGate.authorize")


class AllocationGate:
    """Authorizes allocation requests against the owner's public keys, and records every attempt."""

    def __init__(self, owner_keys: Mapping[str, bytes], *, journal_path: Path, max_ttl_s: float,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not owner_keys:
            raise ValueError("no owner keys: nobody could authorize anything")
        if not max_ttl_s > 0:
            raise ValueError("max_ttl_s must be > 0")
        self._keys = {_ident("key id", k): _load_public(v) for k, v in owner_keys.items()}
        self._journal = _Journal(journal_path)
        self._max_ttl_s = max_ttl_s
        self._clock = clock

    def _refuse(self, req: AllocationRequest, key_id: str, reason: str, canonical: bytes) -> AllocationRefused:
        try:
            self._journal.append({"event": "refused", "ts": self._clock().isoformat(),
                                  "request_id": req.request_id, "key_id": key_id, "reason": reason,
                                  "fingerprint": fingerprint(canonical)})
        except OSError as e:
            return AllocationRefused(f"{reason} (and the refusal could not be recorded: {e})")
        return AllocationRefused(reason)

    def authorize(self, req: AllocationRequest, key_id: str, signature: bytes) -> AuthorizedAllocation:
        canonical = canonical_bytes(req)
        now = self._clock()
        if key_id not in self._keys:
            raise self._refuse(req, key_id, "not an owner key", canonical)
        expires = _utc_text(req.expires_at)
        if now >= expires:
            raise self._refuse(req, key_id, "request has expired", canonical)
        if (expires - now).total_seconds() > self._max_ttl_s:
            raise self._refuse(req, key_id, f"expiry is more than {self._max_ttl_s:g}s away", canonical)
        if any(r.get("event") == "authorized" and r.get("request_id") == req.request_id
               for r in self._journal.rows()):
            raise self._refuse(req, key_id, "this request id was already authorized", canonical)
        if not _verifies(self._keys[key_id], canonical, signature):
            raise self._refuse(req, key_id, "signature does not verify", canonical)
        plan = build_plan(req)
        at = now.isoformat()
        try:
            self._journal.append({
                "event": "authorized", "ts": at, "request_id": req.request_id, "key_id": key_id,
                "fingerprint": fingerprint(canonical), "canonical": canonical.decode("utf-8"),
                "signature": base64.b64encode(signature).decode("ascii"), "request": req.to_json(),
                "lines": [[m, c] for m, c in plan.lines]})
        except OSError as e:
            raise AllocationRefused(f"could not record the authorization ({e}); nothing was authorized") from e
        return AuthorizedAllocation(req, plan, key_id, fingerprint(canonical), at, _GATE_TOKEN)


def verify_journal(path: Path, owner_keys: Mapping[str, bytes]) -> list[str]:
    """Offline audit. Returns problems; an empty list means the chain, signatures and plans all hold."""
    problems: list[str] = []
    keys = {k: _load_public(v) for k, v in owner_keys.items()}
    prev = GENESIS
    for i, row in enumerate(_Journal(path).rows()):
        if row.get("seq") != i:
            problems.append(f"row {i}: sequence is {row.get('seq')!r}")
        if row.get("prev_hash") != prev:
            problems.append(f"row {i}: chain broken (prev_hash does not match the row before)")
        if row.get("row_hash") != _row_hash(row):
            problems.append(f"row {i}: contents were altered (row_hash does not match)")
        prev = row.get("row_hash", "")
        if row.get("event") != "authorized":
            continue
        try:
            req = AllocationRequest.from_json(row["request"])
            sig = base64.b64decode(row["signature"])
        except Exception as e:
            problems.append(f"row {i}: authorized row is unreadable ({e})")
            continue
        if row.get("canonical") != canonical_bytes(req).decode("utf-8"):
            problems.append(f"row {i}: stored request does not match the signed bytes")
        key = keys.get(row.get("key_id", ""))
        if key is None:
            problems.append(f"row {i}: signed by {row.get('key_id')!r}, which is not an owner key")
        elif not _verifies(key, canonical_bytes(req), sig):
            problems.append(f"row {i}: signature does not verify (NOT signed by the owner)")
        if [list(x) for x in build_plan(req).lines] != row.get("lines"):
            problems.append(f"row {i}: recorded lines differ from the plan the request produces")
    return problems
