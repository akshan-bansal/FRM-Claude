# TradeCard firmware (ESP32-S3)

Skeleton firmware for the approval card. Speaks the same REST protocol as
`scripts/approval_card_sim.py`.

## Product profile (what this skeleton targets)

- **Display**: Nokia-5110-class B&W pixel LCD (PCD8544, 84×48 px). SPI + D/C.
- **Input**: 5-key D-pad — UP / DOWN / LEFT / RIGHT + circular CENTER button.
  - `RIGHT` = **ACCEPT**, `LEFT` = **DECLINE**, `CENTER` = detail view (TODO).
  - `UP` / `DOWN` scroll the **passbook** when no live prompt is on screen.
- **Passbook**: persistent ring buffer (32 entries) of recent verdicts,
  saved to NVS. The card behaves like a printed savings passbook — you can
  flip through the last N trades any time.
- **Broker awareness**: prompts carry a `broker` field (`ib` / `kraken` /
  `questrade`). It is displayed and signed, so a MITM cannot silently
  redirect the order between brokerages. Canadian users typically pair
  Interactive Brokers (equities/options/futures) with Kraken (crypto);
  Questrade stays supported for the existing router.
- **Real-time intel**: prompts carry a short `thesis` string sourced from
  the VS investment engine so the tap-to-approve moment shows *why* the
  algorithm wants the trade, not just *what*.

## What's here

| File | Purpose |
|---|---|
| `main/main.c` | Wi-Fi bring-up, Ed25519 keys (NVS), passbook, PCD8544 driver, 5-key input, long-poll, sign + respond |
| `main/Kconfig.projbuild` | menuconfig entries for SSID / PSK / shim URL / card_id |
| `main/CMakeLists.txt` | Component deps: `esp_http_client`, `json`, `libsodium`, `driver`, `nvs_flash` |
| `sdkconfig.defaults` | Target esp32s3, 8MB flash, TLS via mbedTLS |
| `CMakeLists.txt` | Top-level project file |

## Build

```
. $IDF_PATH/export.sh                  # or export.ps1 on Windows
cd firmware/tradecard
idf.py add-dependency "espressif/libsodium^1.0.20"
idf.py set-target esp32s3
idf.py menuconfig                      # fill in "TradeCard" menu
idf.py build flash monitor
```

## Wire-up

| Signal | Pin | Notes |
|---|---|---|
| UP | GPIO4 | active-low, internal pull-up |
| DOWN | GPIO5 | active-low, internal pull-up |
| LEFT (DECLINE) | GPIO6 | active-low, internal pull-up |
| RIGHT (ACCEPT) | GPIO7 | active-low, internal pull-up |
| CENTER | GPIO8 | active-low, internal pull-up |
| LCD MOSI | GPIO11 | SPI2 |
| LCD SCLK | GPIO12 | SPI2 |
| LCD CE | GPIO10 | chip-select |
| LCD D/C | GPIO9 | data/command select |
| LCD RST | GPIO13 | reset |
| LCD BL | GPIO14 | backlight (active-high) |
| Secure element (opt) | I²C0 | move `g_sk` to ATECC608A when you add one |

## Protocol contract (must match `src/trading_live_claude/execution/approval.py`)

- Register: `POST /card/register` with `{card_id, pubkey_pem}` — Ed25519 SPKI PEM.
- Poll: `GET /intents/pending` → `{prompts: [Prompt]}`.
- Prompt fields the card should display: `broker`, `action`, `symbol`,
  `shares`, `notional_usd`, `thesis` (and `intel_ref` for a full writeup
  the phone bridge can fetch). The `expires_at` field drives the countdown.
- Sign: exactly the `canonical` string field of the prompt, as bytes.
  Canonical order is `broker|action|symbol|shares|entry|notional|account|intent_id|nonce`.
- Respond: `POST /intents/{id}/response` with
  `{decision: "ACCEPT"|"DECLINE", card_id, signature: base64(ed25519_sig)}`.

The shim rejects unknown cards, bad signatures, expired intents, and replays.

## Passbook layout on the LCD

```
== Passbook ==
A ib     BUY XIC.TO 12sh $372
D kraken SELL BTCUSD 0.05 $2100
A ib     BUY AAPL 5sh $920
X quest  BUY VFV.TO 4sh $520
UP/DN scroll
```

`A` = accepted, `D` = declined, `X` = expired. Broker column tells you at
a glance which brokerage the trade went to.

## What this skeleton is NOT

- Not TLS-pinned. Add `esp_transport_ssl_set_client_cert_data` and pin the
  shim's cert if the card ever crosses an untrusted network.
- No real font. `lcd_puts` currently mirrors to UART; wire in a 5×7 font
  (Adafruit-GFX / u8g2 style) to actually paint pixels into `s_fb`.
- Not power-managed. Real card needs `esp_pm_lock` and deep-sleep between
  polls to hit a card-form-factor battery budget.
- Not tamper-hardened. Storing `g_sk` in NVS is fine for breadboard; move
  it to ATECC608A or the ESP32-S3 DS peripheral for production.
- Center-button detail view is not yet implemented (renders the same
  screen; hook it up to fetch `intel_ref` from the shim and paginate the
  thesis).

## End-to-end without silicon

Use `scripts/approval_card_sim.py` — it speaks the same protocol, now with
broker + thesis rendering. Post an intent with a broker choice:

```
curl -X POST http://127.0.0.1:8787/intents \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"XIC.TO","action":"Buy","shares":12,"entry":31.05,
       "stop":30.40,"target":32.10,"strategy":"vs_engine_v3",
       "risk_dollars":7.80,"account_number":"paper-001",
       "broker":"ib","thesis":"BoC pause + oil down; TSX cyclicals lag",
       "ttl_seconds":90}'
```
