# P2P Ad Manager

Telegram-controlled advertisement & price manager for **Binance P2P, OKX P2P and ByBit P2P**.
It creates and updates your own merchant advertisements for fiat/crypto pairs (UAH/USDT,
UAH/USDC, PLN/USDT, PLN/USDC, …) and keeps every price under a hard **cap_rate** ceiling.

* **Runtime dependencies: none** — pure Python standard library (3.11+).
* **Telegram access: one user only** (`TELEGRAM_OWNER_ID`). Every other sender is ignored
  silently (audit-logged), group chats are refused, and the bot **never accepts or
  downloads files**.
* **Scenarios are data**: one JSON blueprint per market scenario, one custom strategy per
  trading pair.

---

## 1. Install

```bash
python -m pip install -r requirements-dev.txt   # optional: pytest + coverage (tests only)
cp .env.example .env                            # then edit .env
```

The bot itself needs no third-party packages.

## 2. Configure `.env`

```dotenv
TELEGRAM_BOT_TOKEN=123456:AA...            # BotFather token
TELEGRAM_OWNER_ID=123456789                # the ONLY Telegram user id allowed to talk to the bot
TELEGRAM_API_BASE=https://api.telegram.org # override only for tests/stubs

# One block per account. "<PLATFORM>_<N>_<FIELD>"; the account is addressed as <Platform>#N.
BINANCE_1_API_KEY=...                      # -> Binance#1
BINANCE_1_SECRET_KEY=...
BINANCE_2_API_KEY=...                      # -> Binance#2
BINANCE_2_SECRET_KEY=...
OKX_1_API_KEY=...                          # -> Okx#1
OKX_1_SECRET_KEY=...
OKX_1_PASSPHRASE=...
BYBIT_1_API_KEY=...                        # -> Bybit#1
BYBIT_1_SECRET_KEY=...
```

`verify-config` fails loudly when a scenario references an account that is missing from
`.env`, when a required credential is absent, or when `TELEGRAM_OWNER_ID` is not a positive
integer. If a venue's P2P advertisement API needs a logged-in web session instead of an API
key, supply `{PLATFORM}_{N}_SESSION_COOKIE` / `{PLATFORM}_{N}_CSRF_TOKEN` for that account.

### Creating the exchange keys

**Bybit has the non-obvious requirement.** In *Account → API → Create New Key* pick
**Read-Write**, set the IP mode to **“Only IPs with permissions granted”** and add the bot
host's public IP address, *then* tick **Fiat trading → Ads (“Check, add or modify ads”)**.
While “No IP restriction” is selected Bybit **disables** the Fiat trading scope — its own
notice reads *“Fiat trading and withdrawal services are restricted”* — and the checkbox
cannot be ticked, leaving a key that is useless for P2P. The IP whitelist also stops the key
from expiring after 3 months.

**Binance** needs a key that may call the C2C Agent API (`/sapi/v1/c2c/agent/ads/*`). The
adapter resolves the account's payment-method IDs on first use, so the key must also be able
to read them (`_resolve_trade_methods`). Binance displays the HMAC secret exactly once, behind
a passkey confirmation.

**OKX** needs *Read* + *Trade* and a **passphrase** (`OKX_1_PASSPHRASE`). The passphrase is
part of every signed request, so it must be the one chosen when the key was created; OKX
confirms the key before validating anything else.

Every venue answers the adapters' signed requests with an auth error naming only the key
(`{"code":-2008,"msg":"Invalid Api-Key ID."}` on Binance, `{"code":"50111"}` on OKX,
`{"ret_code":10003}` on Bybit). That is the fast way to check a key end to end: run
`python run.py verify-config`, then `/setcap <PAIR> <RATE>` in the bot — the reply reports the
attempted ads and the per-account errors.

## 3. Scenarios (JSON blueprints)

```jsonc
{
  "version": 1,
  "name": "uah",
  "fiat": "UAH",
  "strategy": "fixed_spread",          // "fixed_spread" | "market_middle"
  "parser": {"enabled": false, "interval_minutes": 25, "cron": "*/25 * * * *"},
  "defaults": {"min_amount": "1000", "max_amount": "200000",
               "payment_methods": ["Monobank"], "price_offset": "0"},
  "pairs": [
    {"pair": "UAH/USDT", "anchor": true,  "accounts": ["Binance#1", "Binance#2", "Okx#1", "Bybit#1"]},
    {"pair": "UAH/USDC", "anchor": false, "linked_to": "UAH/USDT", "accounts": ["Binance#1", "Bybit#1"]}
  ]
}
```

Per pair (and optionally per platform) the blueprint controls: `anchor`, `linked_to`,
`accounts`, `min_amount`, `max_amount`, `payment_methods`, `price_offset`, `enabled`,
`platforms.<name>.source` and per-platform `filters`.

### Price sources

| source | meaning |
|---|---|
| `base_rate` | the rate you set with `/setbase` for that pair |
| `base_rate_minus_spread` | the anchor pair's `base_rate` minus the **hardcoded** platform spread |
| `market_middle` | middle of the filtered competitor advertisements (parser) |
| `copy:<Platform>` | the price already computed for the same pair on another platform |

### UAH scenario (`scenarios/uah.json`) — `fixed_spread`

* Prices come **only** from `base_rate`; competitor data is never used.
* `UAH/USDC` = `UAH/USDT` (base) − hardcoded spread per platform:
  **Binance 0.25 UAH**, **OKX 0.01 UAH**, **ByBit 0.01 UAH**. Example with base 47.00:
  Binance USDT 47.00 / USDC 46.75 · OKX USDT 47.00 / USDC 46.99 · ByBit USDT 47.00 / USDC 46.99.
* The spread is a constant in `p2pbot/constants.py` — a blueprint that tries to declare its
  own spread is rejected.

### PLN scenario (`scenarios/pln.json`) — `market_middle`

* A separate **cron parser** runs every **25 minutes** (`parser.interval_minutes`/`cron`),
  fetching competitor advertisements and setting each price to the **middle of the filtered
  range**.
* Binance filter: merchants only, `monthOrderCount > 500`, `positiveRate > 0.97`,
  `monthFinishRate > 0.94`.
* OKX filter: merchants only.
* ByBit automatically **copies the Binance price** for the same pair.

### The cap is absolute

`/setcap <PAIR> <RATE>` stores `cap_rate`. Every computed price is clamped to it before it
is sent to a venue — cap beats every other rule, including the hardcoded UAH spread. A pair
without a stored cap is **not published at all** (fail-closed).

### Partial failures are isolated, never silent

A venue can legitimately have no usable competitor data for a pair (OKX currently returns
zero USDC ads for UAH and PLN). Those entries are **skipped per (pair, platform)** — every
other advertisement is still corrected — and reported as `skipped <n>` lines by `/rates`,
`/status` and `/publish`. A skipped entry never leaves a stale price published by the bot
itself; it simply stops refreshing that one advertisement until data returns.

### Optional `.env` switches

```dotenv
SCENARIO=uah                 # blueprint activated at startup (also settable with /scenario)
REFRESH_INTERVAL_MINUTES=10  # extra compute+publish cycle; empty/0 disables it
```

## 4. Run

```bash
python run.py verify-config                # validate .env + all blueprints (exit 1 on error)
python run.py rates    --scenario uah      # print computed prices, publish nothing
python run.py parser   --scenario pln      # one competitor-parser pass (--dry-run: print only)
python run.py publish  --scenario uah --dry-run
python run.py tick                         # one full cycle: due parser jobs -> compute -> publish
python run.py bot                          # long-poll the Telegram bot (+ 25 min parser job)
```

`SCENARIO=<name>` (or `--scenario`) is required: without an active scenario the commands exit
`1` with `no active scenario; available: …`, and the first `/setbase` + `/setcap` for a pair
is what gives it something to publish — until then it refuses rather than guessing a price.

Run `bot` under any supervisor (systemd, Windows Task Scheduler, `nssm`, Docker). Alternatively
schedule `python run.py tick` every minute and keep `bot` always on.

Exit codes: `0` success, `1` configuration/state fault (including `tick`/`publish` when
problems — missing cap, missing rate, no market data — blocked *every* advertisement), `2`
unexpected runtime error. `tick`/`publish` still exit `0` whenever at least one
advertisement was pushed, so a single data-less venue never looks like a failure.

## 5. Telegram commands (owner only)

```
/start /help                     usage
/setbase UAH/USDT 47.00          store base_rate
/setcap  UAH/USDT 47.10          store cap_rate (hard ceiling)
/rates                           base/cap table + computed prices
/scenarios                       list blueprints
/scenario uah                    activate a blueprint
/parse [UAH/USDT]                run the parser once
/publish [--dry]                 create/update advertisements now
/pause [PAIR]  /resume [PAIR]    deactivate / reactivate advertisements
/status                          scenario, rates, market ages, scheduler, last publish
/version
```

## 6. Security model

* Only `TELEGRAM_OWNER_ID` can interact; other senders get **no reply at all** and one audit
  log line (`audit: rejected not-owner update=… from=…`).
* Any message carrying a file or binary payload (`document`, `photo`, `video`, `audio`,
  `voice`, `sticker`, `animation`, `contact`, `location`, …) is **silently ignored** — no
  reply, audit log only, and the bot never calls `getFile` and never downloads anything.
  This holds for the owner's own messages too: nobody can make the bot process or echo a file.
* Non-private chats are refused even for the owner; the owner is rate-limited (20 msg/60 s,
  answered with `Too many requests.`).
* Secrets from `.env` are never printed; adapter logs and errors are redacted.
* Unknown commands and malformed arguments return usage text, never tracebacks.

## 7. Tests

```bash
python -m pytest                        # full suite (hermetic: no network, no real APIs)
python -m pytest --cov=p2pbot --cov-report=term-missing
```

The suite is the quality gate for this project: **1051 tests, ≥98 % line coverage of the
`p2pbot` package** (currently 99 %), all hermetic — the venue adapters are driven through an
injected `Transport`, so no test opens a socket.

`scripts/telegram_stub_smoke.py` runs the real bot loop against a local Telegram API stub and
proves that foreign senders and attachments get no response. `scripts/smoke_live.py` fetches
the public P2P endpoints (read-only, no credentials) to validate the adapters against live
venue data; see `docs/research/` for the captured contracts.

## 8. Known venue constraints

* **Bybit** serves `403 Access Denied` (Akamai) for `www.bybit.com/x-api/...` to non-browser
  clients; the PLN ByBit price therefore comes from `copy:Binance`, exactly as specified.
  `api.bybit.com` itself is reachable (its `/v5/p2p/*` endpoints answer signed requests), so
  publishing is unaffected.
* **Binance** advertises through the C2C Agent API (`/sapi/v1/c2c/agent/ads/*`). Its create
  body must be complete — the adapter sends the same field set as Binance's web client — with
  `classify: "profession"` (a merchant account is refused with `83749` for `"mass"`) and
  `tradeType` spelled `"SELL"`/`"BUY"`. Updates re-resolve the payment methods into the write
  shape because `getDetailByNo` answers with the read shape only (otherwise `83664`). All four
  findings are live-verified and pinned by tests.
* **Binance** returns the *mirrored* `adv.tradeType` in search results (the taker's side):
  request `tradeType=SELL` for the ask side we compete on and never derive a side from that
  response field. `docs/research/ground-truth-probes.md` documents the live evidence.
* **OKX** ignores the `userType` query parameter; merchant detection uses `creatorType` /
  `merchantId`. Both sides are live-verified: the public search returns real ads, and the
  private paths `POST https://www.okx.com/api/v5/p2p/ad/create` / `…/ad/update` answer
  `HTTP 401 {"code":"50111","msg":"Invalid OK-ACCESS-KEY"}` to a correctly-signed request with
  a placeholder key — the path and the `OK-ACCESS-*` signing are accepted, only the key is
  missing. The *body* field names still cannot be confirmed from outside (auth precedes body
  validation), so they stay centralized in one mapping in `p2pbot/exchanges/okx.py`, marked
  UNCONFIRMED, to be checked against the merchant reference before first live use.
* **Binance/Bybit private ad management** is production-verified at the transport level: with
  placeholder credentials, `POST api.binance.com/sapi/v1/c2c/agent/ads/{post,update,listWithPagination,getDetailByNo}`
  returns `{"code":-2008,"msg":"Invalid Api-Key ID."}` and
  `POST api.bybit.com/v5/p2p/item/{create,update}` returns `{"ret_code":10003,"ret_msg":"API key is invalid."}`
  — the endpoints and the `X-MBX-APIKEY` / `X-BAPI-*` signing schemes are live-verified, and
  valid credentials are the only missing ingredient. Note `api.bybit.com` is reachable even
  though the `www.bybit.com/x-api/...` route is Akamai-blocked; request shapes come from the
  venues' own documentation and are pinned by signature-vector tests.
* Any rate change (and every 25-minute parser cycle for PLN) re-prices and re-pushes; a
  venue that reports a failure for one account is isolated and reported per account.
