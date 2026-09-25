# P2P Ad Manager — Architecture Spec (authoritative)

Owner: architect. Implementers MUST follow this document exactly. Any deviation must be
reported back, never silently introduced.

> **Scope change (2026-09-25).** The bot no longer publishes scenario ads and never touches
> sell ads. Removed: automatic publishing (`AdPublisher.publish`, `build_ad_spec`,
> `refresh_prices`, `set_active`, the `prices` refresh job, CLI `publish`/`tick`, Telegram
> `/publish`/`/pause`/`/resume`, and later `/parse`/`/status`/`/version`), Binance sell-ad
> payment resolution, and the UAH `fixed_spread` scenario with `UAH_SPREAD` and
> `base_rate_minus_spread`. Live ads are read
> with `AdPublisher.fetch_own_ads` and existing **buy** ads are edited with
> `AdPublisher.edit_ad`; the PLN buy ads are repriced by `p2pbot/edit_queue.py`
> (`python run.py pln-edits`). Sections below are updated accordingly.

## 0. Hard constraints

- **Runtime deps: Python standard library ONLY** (>=3.11; target 3.14 on this box).
  No requests/httpx/aiohttp/pydantic/aiogram/dotenv. HTTP via `urllib.request` behind an
  injectable `Transport` interface. This keeps the bot install-free and unit-testable.
- **Money math: `decimal.Decimal` only.** Never `float` for prices, rates, thresholds.
  Parse from `str` or `int`, never from `float`.
- **Windows-first** (dev box is win32). No POSIX-only calls (`fcntl`, `os.fork`, signals for
  scheduling). Atomic file writes via `os.replace`.
- Tests must never touch the network or real Telegram/exchange APIs.
- Every module: type hints, module docstring, no bare `except:`.

## 1. Project layout (exact)

```
p2p-ad-bot/
  run.py                     # thin entrypoint: from p2pbot.cli import main; main()
  .env.example               # documented template (no real secrets)
  .gitignore
  requirements-dev.txt       # pytest, pytest-cov (dev only)
  README.md                  # operator manual
  docs/SPEC.md               # this file
  docs/research/*.md         # investigator findings per exchange (input for adapters)
  scenarios/pln.json         # blueprint: PLN market-middle scenario (+ ByBit copy)
  scripts/smoke_live.py      # live public-endpoint smoke (my verification, not part of test suite)
  var/                       # runtime state (state.json, market.json, ads.json, bot.log) - gitignored
  p2pbot/
    __init__.py
    constants.py
    models.py
    errors.py
    config.py
    blueprint.py
    rates.py
    market.py
    engine.py
    publisher.py
    edit_queue.py
    scheduler.py
    cron.py
    logging_setup.py
    cli.py
    exchanges/
      __init__.py            # ADAPTERS registry: platform -> adapter class
      base.py                # Transport, HttpRequest/HttpResponse, ExchangeAdapter ABC, Filters
      binance.py
      okx.py
      bybit.py
    telegram/
      __init__.py
      api.py                 # TelegramAPI (stdlib long polling)
      security.py            # owner-only gate + attachment rejection
      handlers.py            # command router
      bot.py                 # BotRunner: poll loop wiring API+security+handlers
  tests/                     # pytest suite (QA owner)
```

## 2. Domain vocabulary

- `Pair` — `"UAH/USDT"` (fiat/crypto, fiat first). Canonical key of everything.
- `base_rate` — per-pair reference rate entered by the Telegram owner.
- `cap_rate` — per-pair hard ceiling entered by the Telegram owner. An advertisement price
  MUST NEVER exceed it. This is enforced in `engine.quantize/clamp`, and re-asserted in
  `publisher` before any update request is built (defence in depth).
- `anchor` — pair whose price is derived directly from `base_rate`.
- `platform` — one of `binance`, `okx`, `bybit` (lowercase).
- `account id` — `"Binance#1"`, `"Binance#2"`, `"Okx#1"`, `"Bybit#1"` (display form,
  `{Platform.capitalize()}#{index}`).

## 3. `.env` schema and account discovery

Keys (explicit, resolved case-insensitively from the process env / `.env` file):

| key | meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token. Required for `bot` command. |
| `TELEGRAM_OWNER_ID` | **Integer** Telegram user id of the ONLY allowed operator. |
| `TELEGRAM_API_BASE` | optional, default `https://api.telegram.org` (test stub override). |
| `STATE_PATH` | optional, default `var/state.json`. |
| `MARKET_PATH` | optional, default `var/market.json`. |
| `ADS_PATH` | optional, default `var/ads.json`. |
| `SCENARIOS_DIR` | optional, default `scenarios`. |
| `LOG_PATH` | optional, default `var/bot.log`; empty string = console only. |
| `LOG_LEVEL` | optional, default `INFO`. |
| `SCENARIO` | optional; name of the blueprint activated at startup (`/scenario` can change it later). |

`Settings.raw` is the **complete merged mapping** (process env overlaid on the `.env` file),
including keys the loader does not interpret itself, so optional switches stay reachable.

Accounts are discovered generically: any key matching

```
^([A-Za-z]+)_(\d+)_([A-Z0-9_]+)$      e.g. BINANCE_1_API_KEY, OKX_2_PASSPHRASE, BYBIT_1_SECRET_KEY
```

becomes `Account(platform=<lowercased platform>, index=<int>, credentials={FIELD: value})`
with id `Binance#1`. Fields are stored uppercase and also mirrored lowercase in
`Account.credentials` (`API_KEY` and `api_key` both resolve through `Account.credential("api_key")`).

Required credentials per platform (validated by `Settings.validate_accounts()`):

- `binance`: `API_KEY`, `SECRET_KEY`. Optional: `CSRF_TOKEN`, `SESSION_COOKIE` (P2P web session).
- `okx`: `API_KEY`, `SECRET_KEY`, `PASSPHRASE`. Optional: `SESSION_COOKIE`, `CSRF_TOKEN`.
- `bybit`: `API_KEY`, `SECRET_KEY`.

`.env.example` MUST contain two Binance accounts, one Okx, one Bybit, and document that
session-based credentials are supplied when the venue requires them.

Parsing rules for `.env` (`config.load_dotenv`): `KEY=VALUE` lines, `#` comments, blank
lines ignored, surrounding single/double quotes stripped, `export ` prefix tolerated,
values never interpolated. Missing file → empty mapping (no error). Precedence:
process environment > `.env` file.

## 4. Blueprint JSON schema

A blueprint is the *custom trading scenario*. One file per scenario. Optional sibling file
`<scenario>.json` only — see `scenarios/`.

```jsonc
{
  "version": 1,
  "name": "pln",
  "fiat": "PLN",                       // required, "UAH" | "PLN"
  "strategy": "market_middle",         // the only strategy
  "parser": {                          // optional
    "enabled": false,                  // PLN blueprints: true
    "interval_minutes": 25,            // MUST be 25 unless explicitly overridden
    "cron": "*/25 * * * *"             // optional 5-field cron; when present it drives the parser
  },
  "defaults": {
    "min_amount": "1000",
    "max_amount": "200000",
    "payment_methods": ["Monobank"],
    "price_offset": "0",
    "filters": {                       // optional per-platform override of hardcoded filters
      "binance": {"user_type": "merchant", "min_month_order_count": "500",
                   "min_positive_rate": "0.97", "min_month_finish_rate": "0.94"},
      "okx": {"user_type": "merchant"}
    }
  },
  "pairs": [
    {
      "pair": "UAH/USDT",
      "anchor": true,
      "accounts": ["Binance#1", "Binance#2", "Okx#1", "Bybit#1"],
      "min_amount": "1000",            // optional, falls back to defaults
      "max_amount": "200000",
      "payment_methods": ["Monobank", "PrivatBank"],
      "price_offset": "0",             // optional Decimal added to computed price (may be negative)
      "enabled": true,
      "platforms": {                   // optional; per-platform source / account override
        "bybit": {"source": "copy:Binance"}
      },
      "filters": { }                   // optional, same shape as defaults.filters
    },
    { "pair": "UAH/USDC", "anchor": false, "linked_to": "UAH/USDT", "accounts": ["..."] }
  ]
}
```

### 4.1 Source resolution (defaults when `platforms` is absent)

`strategy = "market_middle"` (the only strategy):
- `binance`, `okx` → `"market_middle"`.
- `bybit` → `"copy:Binance"`.

### 4.2 Validation (loader MUST raise `BlueprintError` with a precise message)

- `version` must be `1`; `fiat` in `{UAH, PLN}`; `strategy` known; `name` non-empty.
- every `pair` parses as `FIAT/CRYPTO` and its fiat equals the blueprint fiat.
- `min_amount > 0`, `max_amount >= min_amount`, `price_offset` parseable Decimal.
- each account id matches `^[A-Za-z]+#\d+$` and its platform is a known platform.
- non-anchor pair without `linked_to` → error; `linked_to` must reference a pair
  present in the same blueprint and marked `anchor: true`.
- `copy:<Platform>` source requires that platform's plan to exist for the same pair and to
  itself not be a `copy:` source (no copy chains).
- duplicate `pair` entries → error.
- `parser.interval_minutes` > 0.

`Blueprint.pairs` is ordered: anchors first, then linked pairs (topological by
`linked_to`), then copy-derived platforms inside `compute()`.

## 5. Hardcoded constants (`p2pbot/constants.py`)

```python
PLATFORMS = ("binance", "okx", "bybit")
PRICE_TICK = {"UAH": Decimal("0.01"), "PLN": Decimal("0.01")}
DEFAULT_PARSER_INTERVAL_MINUTES = 25
BINANCE_FILTERS = Filters(user_type="merchant", min_month_order_count=Decimal("500"),
                          min_positive_rate=Decimal("0.97"), min_month_finish_rate=Decimal("0.94"))
OKX_FILTERS = Filters(user_type="merchant")
SIDE_SELL = "sell"
```

Threshold semantics are **strict** (`>`), matching "more than 500", "positiveRate > 97%",
"monthFinishRate > 94%".

## 6. Rate store (`p2pbot/rates.py`)

```python
class RateStore:
    def __init__(self, data: Mapping[str, dict] | None = None, path: Path | None = None) -> None
    def set_base(self, pair: Pair, rate: Decimal) -> None      # rate > 0 required
    def set_cap(self, pair: Pair, rate: Decimal) -> None       # rate > 0 required
    def base(self, pair: Pair) -> Decimal | None
    def cap(self, pair: Pair) -> Decimal | None
    def clear(self, pair: Pair) -> None
    def as_dict(self) -> dict                                  # {"base": {...}, "cap": {...}}
    @classmethod
    def from_dict(cls, data) -> "RateStore"
    def save(self) -> None                                     # atomic, no-op when path is None
    @classmethod
    def load(cls, path: Path | None) -> "RateStore"
```
Stored values are strings in JSON, parsed with `Decimal`. Unknown input → `ValueError`
(subclass `RateError`) with a message naming the pair. Rates are quantized to the pair
fiat's tick with `ROUND_HALF_UP` on write.

## 7. Price engine (`p2pbot/engine.py`)

```python
@dataclass(frozen=True)
class ComputedAd:
    pair: Pair; platform: str; price: Decimal; source: str
    base: Decimal | None; cap: Decimal; clamped: bool; accounts: tuple[str, ...]

class RateEngine:
    def __init__(self, blueprint: Blueprint, rates: RateStore, market: MarketStore | None = None) -> None
    def compute(self) -> tuple[ComputedAd, ...]          # all enabled pairs, dependency order
    def compute_pair(self, plan: PairPlan, previous: Mapping[tuple[str, str], Decimal]) -> tuple[ComputedAd, ...]
```

Algorithm (exact, per enabled pair plan and per platform in the plan):

1. resolve `source`.
2. `base_rate` → read `rates.base(pair)`; missing → `MissingRateError`.
3. `market_middle` → `market.middle(platform, pair)`; missing snapshot or empty filtered
   range → `MissingMarketDataError`.
4. `copy:<PLATFORM>` → the already-computed price of the same pair on `<PLATFORM>`
   from `previous` (this cycle's results; key `(platform_lower, pair.symbol)`).
   Missing → `MissingMarketDataError`.
5. `price = raw + plan.price_offset`, then `quantize_price(price, pair.fiat)`.
6. `cap = rates.cap(pair)`; missing → `MissingCapError` (an ad MUST NOT be priced
   without a ceiling). If `price > cap`: `price = cap`, `clamped = True`, and a WARNING is
   logged. Cap wins over every other rule.
7. `price <= 0` after clamping → `PriceError` (never publish garbage).
8. emit `ComputedAd` with all accounts of that platform listed for the pair.

`quantize_price(value, fiat)` = `value.quantize(PRICE_TICK[fiat], rounding=ROUND_HALF_UP)`.

Ordering guarantee: anchors → linked pairs → copy platforms, so `previous` always already
holds the Binance price when ByBit copies it.

### 7.1 Partial-failure isolation (`compute_with_problems`) — added after live evidence

Live data shows a venue can legitimately return **zero** usable ads for a pair (OKX
PLN/USDC returned 0 rows on 2026-09-24). Aborting the whole cycle for that would stop every
other advertisement from ever updating, so the engine exposes both modes:

```python
def compute_with_problems(self) -> tuple[tuple[ComputedAd, ...], tuple[str, ...]]
def compute(self) -> tuple[ComputedAd, ...]   # unchanged: raises the FIRST captured error
```

* `compute_with_problems` attempts every (enabled pair, platform, source) entry, **skips**
  the entries that raise an `EngineError` (missing rate/cap/market data/price), records one
  human-readable problem string per skipped entry
  (`"UAH/USDC okx: MissingMarketDataError: no market middle available …"`), and returns the
  successfully computed ads in the usual deterministic order.
* A `copy:<Platform>` source whose source platform failed is skipped too, with its own
  problem string (never a stale or invented price).
* `compute()` keeps the strict contract used by the tests and `/rates`: it delegates to
  `compute_with_problems` and re-raises the **first captured exception object** (so callers
  still see `MissingCapError`/`MissingRateError`/`MissingMarketDataError`/`PriceError`
  exactly as before).
* The cap rule is unaffected: isolation only ever *skips* an advertisement; it never
  publishes a price that the cap did not clear.

## 8. Market parser (`p2pbot/market.py`, `p2pbot/cron.py`, `p2pbot/scheduler.py`)

`MarketStore` keeps `(platform, pair) -> MarketSnapshot` (in-memory + optional JSON file).
A snapshot records `ads` (raw parsed competitor ads), `fetched_at` (UTC datetime),
`filtered` count and the `middle` price (None when the filtered set is empty).

`filter_ads(ads, platform, filters)`:
- `user_type == "merchant"` (case-insensitive) for all platforms.
- Binance only: `month_order_count > 500`, `positive_rate > 0.97`, `month_finish_rate > 0.94`.
- Missing field → ad excluded (fail-closed), logged at DEBUG.

`middle_price(prices)` = `(min + max) / 2` quantized to the pair's fiat tick; `None` if empty.

Endpoints (public, unauthenticated; `GET`/`POST` as noted — confirmed by
`docs/research/*.md`, adapters MUST use the confirmed shapes):

| platform | url | notes |
|---|---|---|
| binance | `POST https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` | JSON body `{fiat, asset, tradeType: "SELL", page, rows, payTypes: [], publisherType: null}` |
| okx | `GET https://www.okx.com/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin` | query `paymentMethod=all&side=sell&userType=all&sortType=price_asc&limit=100&cryptoCurrency=<CRYPTO>&fiatCurrency=<FIAT>&currentPage=1&numberPerPage=5&t=<ms>` |
| bybit | `POST https://www.bybit.com/x-api/fiat/otc/item/recommend/online` | JSON body per research doc |

`cron.py` — full 5-field cron parser (`minute hour dom month dow`), supporting `*`, lists,
ranges, steps, and month/day names; `CronExpression.matches(dt)` and
`CronExpression.next_after(dt) -> datetime` (UTC-naive, raises `CronError` for invalid
expressions and for expressions that never fire, e.g. `0 0 30 2 *`).
`cron.parse_interval(minutes)` returns the equivalent `*/N * * * *` expression.

`scheduler.py` — `Scheduler` holding named `Job(name, func, interval_minutes|None, cron|None,
next_run_at)`; `run_due(now)` executes each due job exactly once and reschedules. Uses
`datetime.now(timezone.utc)` supplied via an injectable clock for tests. The PLN parser job
is registered with `interval_minutes = 25` (or the blueprint `cron` when provided).

Parser pass (`p2pbot/market.py::MarketParser.run_once(blueprint)`):
for each enabled market_middle pair × platform in `{binance, okx}` (copy platforms are not
fetched), fetch raw ads, filter, store snapshot; returns a report
`tuple[MarketFetchResult(platform, pair, fetched, kept, middle, error), ...]`.
Transport failures are captured per fetch (`error` set, snapshot left untouched) — one
failing venue never aborts the pass.

`BotServices.run_parser(pairs=None)` runs that pass and then persists the store
(`MarketStore.save()`, a no-op without a configured path), because the CLI `parser` command
and a later `rates`/`pln-edits` run are *separate processes*: without the write, the second
process would price `market_middle` scenarios from an empty store. The CLI `--dry-run` path
parses into a scratch `MarketStore` and therefore never writes.

## 9. Exchange adapters (`p2pbot/exchanges/`)

### 9.0 Implementation notes (added after the interface freeze — authoritative)

- `Filters` lives in `p2pbot/models.py`; `p2pbot/constants.py` imports it from there.
- `p2pbot/market.py` MUST NOT import `p2pbot.exchanges` at runtime (only under
  `if TYPE_CHECKING:`), because `exchanges/base.py` imports `market.build_snapshot`.
  `MarketParser` receives adapters as an injected mapping.
- `exchanges/__init__.py` is created by the adapter wave; core modules never import it.
- `ATTACHMENT_KEYS`, `SECRET_FIELDS` and `REDACTED` live in `p2pbot/constants.py`.
- The ABC additionally provides the concrete helpers `ensure_success(payload)`,
  `send_private(account, request)`, `parse_ad_list(payload)` and `search_ads(...)`;
  subclasses implement only the abstract hooks (`build_*`, `parse_search_response`,
  `ensure_success`, `parse_ad_response`).
- `MarketFetchResult(platform, pair, fetched, kept, middle, error)` is the return item of
  `MarketParser.run_once(...)`.
- **Binance ad updates are buy-only.** ``build_update_ad_request`` refuses any spec that is
  not a buy ad before a request is built. The update body spells ``tradeType`` ``"BUY"``
  (``ADS_TRADE_TYPE``), not the ``0`` of the private search paths.
- **Binance update body (live-verified).** ``getDetailByNo`` answers with the *read* shape of
  ``tradeMethods`` (``identifier``/``tradeMethodName``/``iconUrlColor``); a buy ad is written
  back with ``{"identifier": ...}`` entries only (the spec's methods when it carries any —
  names resolved against the account's own methods, ids verbatim — else the ad's own).

```python
@dataclass(frozen=True)
class HttpRequest:
    method: str; url: str; params: Mapping[str, str] | None = None
    json_body: Any = None; form_body: Mapping[str, str] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class HttpResponse:
    status: int; body: bytes; headers: Mapping[str, str] = field(default_factory=dict)
    @property
    def json(self) -> Any                     # json.loads(body.decode("utf-8"))

class Transport(Protocol):
    def send(self, request: HttpRequest, *, timeout: float = 15.0) -> HttpResponse: ...

class UrllibTransport:                        # stdlib urllib.request, injectable opener for tests
class ExchangeAdapter(ABC):
    platform: ClassVar[str]
    def __init__(self, transport: Transport | None = None, now: Callable[[], datetime] | None = None)
    # discovery + market data
    @abstractmethod
    def build_search_request(self, pair: Pair, *, side: str = SIDE_SELL, page: int = 1, rows: int = 20) -> HttpRequest
    @abstractmethod
    def parse_search_response(self, payload: Any, pair: Pair) -> tuple[CompetitorAd, ...]
    def search_ads(self, pair, *, side=SIDE_SELL, page=1, rows=20, filters=None) -> MarketSnapshot
    # private ad management
    @abstractmethod
    def build_login_request(self, account: Account) -> HttpRequest | None    # None when not needed
    @abstractmethod
    def build_list_ads_request(self, account: Account, pair: Pair) -> HttpRequest
    @abstractmethod
    def build_update_ad_request(self, account: Account, spec: AdSpec, adv_no: str) -> HttpRequest
    @abstractmethod
    def parse_ad_response(self, payload: Any) -> AdActionResult
```
`AdSpec(pair, price, min_amount, max_amount, payment_methods=(), active=True,
side=SIDE_SELL, quantity=None, payment_ids=(), price_floating_ratio=None)` is the edit
payload (`edit_ad` always fills it from the live **buy** ad);
`quantity`/`payment_ids` are optional (an adapter derives the token amount from
`max_amount / price` and resolves venue payment-method ids — see §9);
`AdActionResult(platform, account_id, pair, adv_no, price, raw)` is the result.
`ExchangeAdapter.parse_ad_result(payload, *, account, pair, spec, adv_no=None)`
is the shared bridge that fills the identity fields around a venue-specific
`parse_ad_response(payload)`, and `AdSpec.active` is the single on/off control that each
adapter maps onto its venue's mechanism.

Adapter rules:
- Signing/headers built in `build_*` so they are unit-testable against fixed vectors.
  Request builders MUST be deterministic given `(account, spec, adv_no, now)`; pass `now`
  through the injectable clock so signature timestamps are reproducible in tests.
- Never log secrets; redact `API_KEY`/`SECRET_KEY`/`PASSPHRASE`/`SESSION_COOKIE` values.
- Raise `TransportError` on transport failure, `ApiError(message, payload)` when the
  venue's own error code/message reports failure (never return success on an error body).
- Where the venue's P2P ad-management API is session/cookie based, `build_*` attaches the
  session cookie + CSRF token from the account credentials and the adapter docstring
  documents exactly which env fields are required.
- `docs/research/<platform>.md` is the source of truth for exact paths/fields; if research
  is inconclusive, implement the documented-by-research shape and note the uncertainty in
  the adapter docstring (never a silent stub).

`exchanges/__init__.py` exposes `ADAPTERS: dict[str, type[ExchangeAdapter]]` and
`build_adapters(transport=None) -> dict[str, ExchangeAdapter]`.

## 10. Publisher (`p2pbot/publisher.py`) and the PLN edit queue (`p2pbot/edit_queue.py`)

```python
class AdStore:                     # (account_id, pair) -> {"adv_no":... , "price":..., "updated_at":...}
    def get(self, account_id, pair) -> AdRecord | None
    def put(self, record: AdRecord) -> None
    def as_dict()/from_dict(); save(); load()

class AdPublisher:
    def __init__(self, adapters: Mapping[str, ExchangeAdapter], settings: Settings,
                 store: AdStore, rates: RateStore, dry_run: bool = False) -> None
    def fetch_own_ads(self, account_ids=None, *, include_closed=False) -> tuple[OwnAdsResult, ...]
    def edit_ad(self, account_id, pair, adv_no=None, *, price=None, price_floating_ratio=None,
                min_amount=None, max_amount=None, quantity=None, payment_methods=None,
                active=None, dry_run=None) -> PublishResult
```
`edit_ad` never creates an ad and only edits **buy** ads: it reads the live ad
(`fetch_own_ads`), keeps every field not passed, re-asserts a stored `cap_rate` (a fixed
price is clamped, a floating ratio estimated above the cap is refused) and refuses sell,
missing, closed or mismatched ads, offline ads without `active=True`, and `active=False`
combined with other changes. Faults become `PublishResult(status="error")`, never raised.

`run_pln_edit_queue(blueprint, engine, publisher, *, parser=None, dry_run=False, pairs=None)`
runs one producer/consumer pass over a `queue.Queue(maxsize=1)`: the producer parses and
prices the PLN pairs (only `pairs` when given; a requested pair missing from the scenario is
reported) and queues one `AdEdit` per online **buy** ad of each pair; the consumer applies
each with `edit_ad(..., price=...)`.

`run_uah_rate_queue(rate, publisher, *, steps, dry_run=False, account_ids=None)` is the
`/setrate` producer on the same queue machinery (`run_edit_queue`): it reads every
configured account's own ads and queues, per account and pair, the online **buy** ads as a
descending ladder (ranked by current price): `UAH/USDT` gets `rate`, `rate - step`,
`rate - 2*step`, … (rate quantized to the UAH tick), `UAH/USDC` the same ladder one step
lower. A cap is optional for the UAH pairs: when one is stored (`caps`, from `/setcap`)
the ladder's top is clamped to it; without one the ladder starts at the rate. (PLN keeps
the fail-closed rule through the engine.) Edits are queued rising-first (top down) then
falling (bottom up), so no ad passes through a neighbour's price.
`steps` come from `p2pbot/uah_config.py` (`STEP`, validated by `load_uah_steps()` into
`BotServices.uah_steps`); the façade method is `BotServices.set_uah_rate(rate, *, dry_run)`.

`p2pbot/cron_config.py` holds the two operator settings `SCHEDULE_TIME` (minutes) and
`PAIRS` (tickers or `PLN/...` symbols). `load_cron_config()` validates them into
`CronConfig(interval_minutes, pairs)` (`ConfigError` naming the setting otherwise);
`build_services` stores it as `BotServices.cron` and registers the `pln-edits` job
(`services.run_cron_edits`) every `interval_minutes`; the `pln-edits` CLI command and
`verify-config` use the same settings.

## 11. Telegram bot (`p2pbot/telegram/`)

### 11.1 Security (non-negotiable)
- Only `TELEGRAM_OWNER_ID` may interact. Every update whose sender id != owner id is
  dropped **without any reply** and written to the audit log
  (`audit: rejected <reason> update=<update_id> from=<from_id>`).
- **The bot never accepts files.** Any message carrying `document`, `photo`, `video`,
  `audio`, `voice`, `video_note`, `sticker`, `animation`, `new_chat_photo`, `contact`,
  `location`, `venue`, `poll`, `invoice`, `successful_payment`, `passport_data` (or any
  `*_file_id` key) is refused **silently** — no reply, audit log only — and
  `getFile`/`download` is NEVER called for any update. Refusal happens *before* command
  dispatch (the owner's own attachments are refused too). Silence is deliberate: nobody
  but the owner can make the bot emit a single message.
- Non-private chats (group/supergroup/channel) are refused even for the owner (silently).
- Rate-limit per owner: > 20 messages / 60 s → the owner is answered `Too many requests.`
  and further messages are ignored until the window frees (sliding window; a refused
  message does not consume the window).
- Secrets never echoed; every reply is redaction-safe.

### 11.2 Commands (router in `handlers.py`, returns reply text; no I/O to Telegram)
```
/start, /help                 usage summary
/setbase <PAIR> <RATE>        store base_rate            (e.g. /setbase PLN/USDT 3.85)
/setcap  <PAIR> <RATE>        store cap_rate
/setrate <RATE> [--dry]       reprice UAH buy ads as a per-account STEP ladder (cap optional)
/getads [<PAIR>|all] [--offline]  list every account's ads (default UAH/USDT + UAH/USDC, online)
/rates                        base/cap table + computed prices per platform
/scenarios                    list blueprint files
/scenario <name>              activate scenario (reload blueprint, validate account refs)
```
Malformed command → `Usage: /setbase <PAIR> <RATE>`; unknown command → help hint.
All handler exceptions are caught and turned into an error reply containing the exception
type/message (never a traceback dump to Telegram).

### 11.3 Polling client (`api.py`)
- `TelegramAPI(base_url, token, transport, timeout=...)` with `get_updates(offset, timeout=25)`,
  `send_message(chat_id, text)`, `set_my_commands(commands)`, `get_me()`.
- Long polling with `offset` bookkeeping; HTTP errors → logged, loop retries with backoff
  (1s → 30s cap). `409 Conflict` and `401` are logged clearly.
- `TELEGRAM_API_BASE` makes the client point at a stub server (used by tests and smoke runs).

### 11.4 `bot.py`
`BotRunner(services, api, access=None, logger=None)`:
- `handle_update(update) -> HandlerResult | None` — authorize (silent refusal for a
  non-owner, no Telegram call at all), else dispatch and send the reply text via the API.
- `run_forever(max_iterations=None, sleep=time.sleep)` — long-poll loop with `offset`
  bookkeeping, backoff 1s → 30s cap on transport/API failures, optional
  `attach_scheduler(scheduler)`; due scheduler jobs (`run_due_jobs()`) run once per poll
  iteration so the PLN parser fires every 25 minutes while the bot polls.

### 11.5 Frozen façade (`p2pbot/services.py`, owner: CLI/publisher developer)

The Telegram layer depends on exactly ONE duck-typed façade object (built by
`services.build_services(settings, transport=None) -> BotServices`) and imports it only
under `if TYPE_CHECKING:` so `p2pbot.telegram` keeps working while `services.py` evolves.

```python
# models.PublishResult (also re-exported by p2pbot.services)
@dataclass(frozen=True)
class PublishResult:
    account_id: str; platform: str; pair: Pair
    status: str            # "updated" | "skipped" | "dry_run" | "error"
    price: Decimal | None = None; adv_no: str | None = None
    error: str | None = None; dry_run: bool = False

@dataclass(frozen=True)
class RateRow:    pair: str; base: Decimal | None; cap: Decimal | None
@dataclass(frozen=True)
class MarketRow:  platform: str; pair: str; middle: Decimal | None; filtered: int
                  fetched_at: datetime | None
@dataclass(frozen=True)
class JobRow:     name: str; next_run_at: datetime | None; last_error: str | None
@dataclass(frozen=True)
class StatusSnapshot:
    version: str; scenario: str | None; fiat: str | None; strategy: str | None
    rates: tuple[RateRow, ...]; prices: tuple[ComputedAd, ...]
    market: tuple[MarketRow, ...]; jobs: tuple[JobRow, ...]
    engine_error: str | None = None            # first problem, or a scenario-level fault
    engine_problems: tuple[str, ...] = ()      # SPEC §7.1 skipped entries, always present

class ScenarioManager:
    def __init__(self, scenarios_dir, state_path=None, active: str | None = None)
    def available(self) -> tuple[str, ...]          # blueprint names, sorted
    def active_name(self) -> str | None
    def activate(self, name: str) -> Blueprint      # validates, persists, returns
    def blueprint(self) -> Blueprint                # ConfigError when none active
    def reload(self) -> Blueprint

class BotServices:                                   # duck-typed façade
    settings: Settings
    version: str
    clock: Callable[[], datetime]
    rates: RateStore
    market: MarketStore
    scheduler: Scheduler
    scenarios: ScenarioManager
    def run_parser(self, pairs: Iterable[str] | None = None) -> tuple[MarketFetchResult, ...]
    def snapshot(self) -> StatusSnapshot
    def set_uah_rate(self, rate: Decimal, *, dry_run: bool = False) -> EditQueueReport
    def get_own_ads(self) -> tuple[OwnAdsResult, ...]
```
Error policy for the façade: `run_parser` never raises for a per-venue failure (it surfaces
in the `MarketFetchResult.error`); the façade DOES raise `EngineError`/`ConfigError` for a
missing scenario so the handler can report it. The façade never pushes an advertisement.

## 12. CLI (`p2pbot/cli.py`, `run.py`)

```
python run.py bot                       # long-poll bot + scheduler thread
python run.py parser --scenario pln     # one market fetch pass (--dry-run prints, no state write)
python run.py rates  --scenario pln     # print computed prices (changes nothing)
python run.py pln-edits --scenario pln [--dry-run]   # reprice the live PLN buy ads
python run.py verify-config             # validate .env accounts + all blueprints, exit 1 on error
```
`pln-edits` honours `--dry-run`. Exit codes: 0 ok, 1 configuration error, 2 runtime error.
Additionally `pln-edits` exits **1 when problems blocked every edit** (nothing edited and
nothing already at its price), so an unattended scheduler never reads that as success.
CLI is covered by tests through `main(argv, env=...)` returning an int.

## 13. Testing requirements (QA)

- `pytest` + `pytest-cov`, all tests hermetic (no sockets). Use `FakeTransport`
  (records requests, replays scripted responses) for adapters.
- Coverage gate: **≥ 98 % line coverage of `p2pbot/`** (measured with
  `python -m pytest --cov=p2pbot --cov-report=term-missing`); `if TYPE_CHECKING` and
  `# pragma: no cover` are allowed only for genuinely unreachable platform branches and
  must be justified in a comment.
- Pass rate must exceed 98 % (target 100 %).
- Required test areas: constants/quantization, config/account discovery (+ error paths),
  blueprint validation, RateStore, engine (every source, cap clamping, missing rates/caps, price offsets, ordering, copy chains), market filtering
  (each Binance threshold boundary: exactly 500 → excluded, 501 → included; 0.97 → excluded,
  0.971 → included; 0.94 → excluded, 0.941 → included), middle computation, cron parsing
  and `next_after`, scheduler due/reschedule, adapters (request building incl. signatures,
  response parsing, error bodies), publisher (edit_ad: live-ad based edits, buy-only,
  cap re-assertion, dry-run, per-account error isolation), the PLN edit queue, telegram security (owner gate, every attachment key,
  non-private chat, rate limit), handlers (each command incl. malformed input),
  bot loop (offset bookkeeping, retry/backoff), CLI (all subcommands, dry-run, exit codes).
- No test may assert implementation trivia (private call counts) unless the behaviour is
  otherwise unobservable; prefer asserting observable outputs/persisted state.

## 14. Definition of done

1. `python run.py verify-config` passes with the shipped `.env.example`-derived config.
2. `python run.py rates --scenario pln` prints PLN/USDT and PLN/USDC prices per platform
   honouring the cap.
3. `python run.py parser --scenario scenarios/pln.json --dry-run` prints filtered ad counts
   and middle prices from live public endpoints (network-permitting), ByBit derived by copy.
4. Bot refuses any non-owner sender and any attachment; owner commands update state.
5. Test suite green with ≥ 98 % coverage of `p2pbot/`.
