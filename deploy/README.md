# Deployment — TradeCard approval shim

Three supported ways to run the shim outside a paper-script daemon
thread. All three run the same open-source Python code; the vendor
operates none of them (see `NEXT_SESSION.md` §11: zero-vendor-infra).

## 1. `pipx install` — the developer path

```
pipx install --pip-args='-e .[shim]' .
tradecard-shim --host 127.0.0.1 --port 8787 \
    --db state/approval.db \
    --auth-token "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Fine for a laptop that stays on during trading hours. Prints the auth
token to stdout — pair the card / simulator with the same string.

## 2. Docker — the home-server / NAS path

```
docker build -f deploy/Dockerfile -t tradecard-shim:v1.0.0 .
docker run --rm -d --name tradecard-shim \
    -p 8787:8787 \
    -v tradecard-state:/data \
    -e TRADECARD_AUTH_TOKEN="$(openssl rand -base64 32)" \
    tradecard-shim:v1.0.0
```

Runs unprivileged (uid 10001). `/data` is the persistent volume for
SQLite + writeups. Healthcheck hits `GET /healthz` every 30 s. Image
publishes to Docker Hub via the sigstore-signed release workflow
(shipped next commit).

## 3. systemd — the always-on Linux box path

```
sudo pipx install --pip-args='-e .[shim]' /path/to/FRM-Claude
sudo useradd --system --home /var/lib/tradecard --shell /usr/sbin/nologin tradecard
sudo mkdir -p /etc/tradecard /var/lib/tradecard
sudo tee /etc/tradecard/env <<EOF
TRADECARD_HOST=127.0.0.1
TRADECARD_PORT=8787
TRADECARD_DB=/var/lib/tradecard/approval.db
TRADECARD_WRITEUP_DIR=/var/lib/tradecard/intel_writeups
TRADECARD_AUTH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
EOF
sudo chown -R tradecard:tradecard /etc/tradecard /var/lib/tradecard
sudo chmod 0600 /etc/tradecard/env
sudo cp deploy/tradecard-shim.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradecard-shim
```

The unit hardens the process — no new privs, no writable-executable
memory, private tmp, kernel-tunables locked, capability bounding set
empty. Break-glass edits go in your distro's override drop-in, never
in the shipped unit.

## Choosing among the three

| | pipx | Docker | systemd |
|---|---|---|---|
| Laptop, ad-hoc | ✓ | | |
| NAS / Synology / home server | | ✓ | |
| Small always-on Linux box | | ✓ | ✓ |
| Bundled RPi-class appliance (mfg allies) | | | ✓ |

The bundled-device path is what most non-technical customers will
use — the manufacturing allies pre-flash a small box with systemd + the
unit above, so the customer plugs USB and pairs a card, nothing else.

## What the shim needs and doesn't need

Needs:
- A writable directory for the SQLite database and the writeup JSON files.
- One TCP port (default 8787).
- An auth token (recommended in all cases; strictly required off-loopback).

Does not need:
- Broker API keys. The shim never talks to a broker. The trading engine
  running elsewhere in the same box (or on the customer's laptop, on
  LAN) is what holds broker credentials.
- Outbound internet, unless firmware OTA delivery is enabled — and even
  then only to GitHub Releases (see the sigstore workflow).
- Root. Every install path above drops to an unprivileged user.
