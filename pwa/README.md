# TradeCard PWA

Static companion app for the TradeCard approval shim. Published from
`.github/workflows/pages.yml` to GitHub Pages on every push to `main`;
also usable from `file://` or any static server on the LAN.

## What it does

- Renders **pending prompts** — the same list the physical card
  long-polls via `GET /v1/intents/pending`, in a browser tab that a
  household member can watch from a phone or another laptop.
- Renders the **passbook** — recent verdicts from `GET /v1/passbook`,
  newest-first, in the same broker × verdict × line × amount × time
  shape the physical card's LCD shows.
- Nothing else. This app never signs an intent. Approval stays on the
  physical card (that's the whole point of the card). The PWA is a
  viewer.

## What it deliberately does not do

- **No vendor request path.** Every call is browser → user's own shim
  on their LAN. GitHub Pages serves the HTML and JS; that's the entire
  vendor footprint (see `NEXT_SESSION.md` §11: zero-vendor-infra).
- **No auth tokens persisted across tabs.** The Bearer token lives in
  `sessionStorage` — closing the tab drops it. Only the shim URL
  survives in `localStorage`, since it's not a secret.
- **No analytics, no telemetry, no font requests.** All CSS + fonts +
  icons are inline. Open Wireshark, verify.
- **No third-party JavaScript.** Everything ships from this one repo.

## Wiring it up

1. Deploy the shim on your LAN (see `deploy/README.md`).
2. Open the Pages URL for this repo (or `pwa/index.html` from a local
   clone).
3. Paste the shim URL and the bearer token it printed at startup.
4. Click Connect.

The `Add to Home Screen` gesture on iOS / Android installs it as a
launchable app icon; the service worker in `sw.js` caches the shell so
it opens instantly on subsequent visits (API calls are never cached).

## When it stops working

Every failure is a shim-side or LAN-side issue, not a PWA issue:
- **Status: error 401** — auth token missing or wrong; the shim is up.
- **Status: error 404 /v1/…** — you're running an older shim; the PWA
  points at the `/v1/` routes and expects `SPEC_VERSION` 1.x.
- **Status: error TypeError: Failed to fetch** — the shim isn't
  reachable from this browser at the URL you typed. Common causes:
  wrong port, wrong host, no LAN route, or the browser blocked mixed
  content (loading `http://…` from an `https://…` page — either serve
  the shim over TLS or serve the PWA over HTTP for LAN use).
