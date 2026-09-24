# Bybit P2P — HTTP contract research

Date of probes: 2026-09-24 (this host, EU/unknown egress, plain HTTP client = the harness `read` tool, which issues **GET only** and follows redirects).

---

## Public market-data endpoint

### A. The route named in the ticket (website/legacy route) — NOT fully verified

`POST https://www.bybit.com/x-api/fiat/otc/item/recommend/online`

* **Observed (this host, `read`/GET):**
  * `GET https://www.bybit.com/x-api/fiat/otc/item/recommend/online` → **HTTP 404**, `Content-Type: text/html`.
  * `GET …/recommend/online?userId=&tokenId=USDT&currencyId=USD&payment=&side=0&size=10&page=1&action=` → **HTTP 404** (`text/html`).
  * `GET https://www.bybit.com/x-api/fiat/otc/item/online?tokenId=USDT&currencyId=USD&side=0&size=5&page=1` → **HTTP 404** (`text/html`).
  * `GET https://www.bybit.com/x-api/fiat/otc/currency/list` → **HTTP 404** (`text/html`).
  * `GET https://api2.bybit.com/fiat/otc/item/recommend/online` → **HTTP 404** (`application/json`).
* **Calibration of what 404 means on Bybit:** `GET https://api.bybit.com/v5/order/create` (a documented POST-only route) → **404**; `GET https://api.bybit.com/v5/market/time` (documented GET) → **200** with `{"retCode":0,"retMsg":"OK","result":{"timeSecond":"1790265956",…}}`. Bybit answers **404 for a method mismatch**, so the 404s above are consistent with “route exists, POST required”, not with “route does not exist”.
* **Architect-side observation (recorded in docs/research/ground-truth-probes.md):** the same URL with a desktop User-Agent returns **HTTP 403 (Akamai "Access Denied")**, i.e. the route exists but is protected against non-browser clients from datacenter IPs.
* **Consequence:** the exact request body of `/x-api/fiat/otc/item/recommend/online` could **not** be confirmed by a live fetch from this environment. **UNCONFIRMED**: the field list `userId / tokenId / currencyId / payment / side / size / page / action` given in the ticket; no primary source for those names was reachable (web search providers all blocked: Google/DuckDuckGo/Ecosia/Mojeek/Startpage returned bot-challenges; grep.app returned 429; GitHub *code* search needs auth). Partial corroboration only: grepping Bybit's own P2P web bundle (`https://www.bybit.com/static/fiat-p2p/js/main.89788ca7.js`) matched the literal `recommend/online` and a body-shaped fragment matching `tokenId:…,currencyId`, i.e. those two keys do exist in the venue's frontend code (bundle is single-line minified; the harness truncates matched lines at 512 bytes so the full body object could not be extracted — **UNCONFIRMED by design of the tooling**).

**What the web UI does instead / what to use programmatically:** use the documented exchange API below (`/v5/p2p/item/online`).

### B. Documented ad-listing endpoint (official docs, real sample)

Source: <https://bybit-exchange.github.io/docs/p2p/ad/online-ad-list> (raw: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/online-ad-list.mdx>)

**Method / URL**

```
POST https://api.bybit.com/v5/p2p/item/online        # mainnet (also api.bytick.com)
POST https://api-testnet.bybit.com/v5/p2p/item/online  # testnet
Content-Type: application/json
```

**Headers in the official example (i.e. the reference treats this as a signed V5 call):**
`X-BAPI-SIGN`, `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP`, `X-BAPI-RECV-WINDOW`.
→ This endpoint is documented **with** V5 authentication headers; whether an anonymous unauthenticated call works is **UNCONFIRMED** (not testable with GET-only tooling). Note the P2P guide states the P2P API is only accessible by General Advertisers or above (<https://bybit-exchange.github.io/docs/p2p/guide>), so even the “Get Ads” listing is advertised as advertiser-gated, not a fully open public feed.

**Request body (all fields are JSON strings except where noted):**

| field | required | observed type | notes |
|---|---|---|---|
| `tokenId` | **true** | string | e.g. `USDT`, `BTC`, `ETH` |
| `currencyId` | **true** | string | fiat, e.g. `HKD`, `USD`, `EUR` |
| `side` | **true** | string | `"0"` = buy, `"1"` = sell |
| `page` | false | string | default `"1"` |
| `size` | false | string | default `"10"`, max `300` |

**Response JSON tree (exact paths, types as observed in the official sample):**

```
ret_code : number           # 0 on success
ret_msg  : string           # "SUCCESS"
result :
  count : int               # total number of ads
  items : array<object>
    [].id                 : string   # ad ID, e.g. "1899658238346616832"
    [].accountId          : string
    [].userId             : string
    [].nickName           : string   # merchant nickname
    [].tokenId            : string
    [].currencyId         : string
    [].side               : number   # 0 buy / 1 sell  (docs table says string; sample shows number)
    [].priceType          : number   # 0 fixed, 1 floating
    [].price              : string   # MONEY/RATE -> STRING, e.g. "0.93"
    [].premium            : string   # "0" when fixed rate
    [].lastQuantity       : string   # tradable/available token qty -> STRING
    [].quantity           : string
    [].frozenQuantity     : string
    [].executedQuantity   : string
    [].minAmount          : string   # min single-trade amount (fiat) -> STRING
    [].maxAmount          : string   # max single-trade amount (fiat) -> STRING
    [].remark             : string
    [].status             : number   # 10 online / 20 offline / 30 completed (status enum from ad-list/ad-detail docs)
    [].createDate         : string   # ms epoch as string, e.g. "1741748793000"
    [].payments           : array<string>  # payment method type IDs, e.g. ["14"], ["377"]
    [].orderNum           : number
    [].finishNum          : number
    [].recentOrderNum     : number   # docs table claims string; SAMPLE SHOWS NUMBER
    [].recentExecuteRate  : number   # docs table claims string; SAMPLE SHOWS NUMBER (e.g. 0)
    [].isOnline           : boolean  # maker currently online
    [].lastLogoutTime     : string   # ms/sec epoch as string
    [].blocked            : string   # "N"
    [].authTag            : array<string>  # "GA" General / "VA" Verified / "BA" Block Advertiser -> MERCHANT FLAG/LEVEL
    [].authStatus         : number
    [].userType           : string   # "ORG" in sample
    [].itemType           : string   # "ORIGIN" | "BULK"
    [].paymentPeriod      : number   # minutes (sample 15)
    [].version            : number
    [].symbolInfo.*       : mixed    # id/exchangeId/orgId/tokenId/currencyId/status(int)/…/orderAutoCancelMinute(int)/token{scale}/currency{scale}
    [].tradingPreferenceSet.hasUnPostAd            : number
    [].tradingPreferenceSet.isKyc                  : number
    [].tradingPreferenceSet.isEmail                : number
    [].tradingPreferenceSet.isMobile               : number
    [].tradingPreferenceSet.hasRegisterTime        : number
    [].tradingPreferenceSet.registerTimeThreshold  : number
    [].tradingPreferenceSet.orderFinishNumberDay30 : number  # required completed orders / 30d
    [].tradingPreferenceSet.completeRateDay30      : string  # required completion rate / 30d, e.g. "95"
    [].tradingPreferenceSet.nationalLimit          : string
    [].tradingPreferenceSet.hasOrderFinishNumberDay30 : number
    [].tradingPreferenceSet.hasCompleteRateDay30   : number
    [].tradingPreferenceSet.hasNationalLimit       : number
```

> **Money/rate types (answers the ticket's explicit question):** every price/amount field (`price`, `minAmount`, `maxAmount`, `lastQuantity`, `quantity`, `frozenQuantity`, `executedQuantity`, `premium`, `tradingPreferenceSet.completeRateDay30`) is a **decimal STRING**. The two “recent activity” counters `recentOrderNum` / `recentExecuteRate` are **JSON numbers** in the real sample (the docs parameter table mislabels them as string) — see sample below (`"recentOrderNum": 0, "recentExecuteRate": 0`).

**Real trimmed sample (copied verbatim from the official docs page; status 200 implied by the docs, see caveat below):**

```json
{
    "ret_code": 0,
    "ret_msg": "SUCCESS",
    "result": {
        "count": 3,
        "items": [
            {
                "id": "1899658238346616832",
                "accountId": "290120",
                "userId": "290118",
                "nickName": "cjmtest",
                "tokenId": "USDT",
                "currencyId": "EUR",
                "side": 0,
                "priceType": 0,
                "price": "0.93",
                "premium": "0",
                "lastQuantity": "10000",
                "quantity": "10000",
                "frozenQuantity": "0",
                "executedQuantity": "0",
                "minAmount": "200",
                "maxAmount": "9300",
                "remark": "1111121212",
                "status": 10,
                "createDate": "1741748793000",
                "payments": ["14"],
                "recentOrderNum": 0,
                "recentExecuteRate": 0,
                "isOnline": true,
                "authTag": ["BA"],
                "userType": "ORG",
                "itemType": "ORIGIN",
                "paymentPeriod": 15,
                "tradingPreferenceSet": {
                    "hasUnPostAd": 0,
                    "isKyc": 1,
                    "orderFinishNumberDay30": 0,
                    "completeRateDay30": "0",
                    "hasOrderFinishNumberDay30": 0,
                    "hasCompleteRateDay30": 0,
                    "hasNationalLimit": 0
                },
                "symbolInfo": { "token": { "tokenId": "USDT", "scale": 4 }, "currency": { "currencyId": "EUR", "scale": 3 } }
            },
            {
                "id": "1899659847717838848",
                "userId": "290118",
                "nickName": "cjmtest",
                "tokenId": "USDT",
                "currencyId": "EUR",
                "side": 0,
                "price": "0.92",
                "lastQuantity": "20000",
                "minAmount": "20",
                "maxAmount": "18400",
                "payments": ["377"],
                "recentOrderNum": 0,
                "recentExecuteRate": 0,
                "authTag": ["BA"],
                "tradingPreferenceSet": {
                    "orderFinishNumberDay30": 60,
                    "completeRateDay30": "95",
                    "hasOrderFinishNumberDay30": 1,
                    "hasCompleteRateDay30": 1
                }
            }
        ]
    }
}
```

**Observed HTTP status caveat:** this sample comes from the venue's own documentation (fetched live over HTTP, page and `.mdx` source both retrieved successfully). It is **not** an HTTP status I observed for a `POST /v5/p2p/item/online` call, because the available tooling cannot issue POST. My only *observed* statuses for that path are `GET /v5/p2p/item/online?tokenId=USDT&currencyId=USD&side=0` → **HTTP 404, `application/json`**.

---

## Private advertisement API

### Base URLs (source: <https://bybit-exchange.github.io/docs/p2p/guide>)

* Testnet: `https://api-testnet.bybit.com`
* Mainnet: `https://api.bybit.com` **and** `https://api.bytick.com`
* Regional hosts: `api.bybit.nl`, `api.bybit.tr`, `api.bybit.kz`, `api.bybitgeorgia.ge`, `api.bybit.ae`, `api.bybit.eu`, `api.bybit.id`, `api.manepa.jp` (JP); Argentina users use `api.bybit.com` + header `x-site-id: ARG_BTL`.
* **UNCONFIRMED:** whether the legacy `/x-api/fiat/...` family is still used for *advertisement management*. It does **not** appear anywhere in the official P2P API reference (the `docs/p2p/` tree contains only `/v5/...` routes), and the official SDK (`bybit_p2p`, now deprecated in favour of `pybit`) maps every advert operation to `/v5/p2p/item/*`. The only `/x-api/fiat/otc/...` route I encountered is the browser-facing ads route in section A above.

### Paths (all `POST`, JSON body, all signed)

| operation | path | source |
|---|---|---|
| list own ads | `POST /v5/p2p/item/personal/list` | <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/ad-list.mdx> |
| one own ad by id | `POST /v5/p2p/item/info` | <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/ad-detail.mdx> |
| create ad | `POST /v5/p2p/item/create` | <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/post-new-ad.mdx> |
| update / **re-online** | `POST /v5/p2p/item/update` (`actionType: "MODIFY"` \| `"ACTIVE"`) | <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/update-list-ad.mdx> |
| remove ad (take-down / delete) | `POST /v5/p2p/item/cancel` body `{"itemId": "…"}` | <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/remove-ad.mdx> |
| list payment methods (needed for `paymentIds` / `payments`) | `POST /v5/p2p/user/payment/list` | official SDK mapping table: <https://github.com/bybit-exchange/bybit_p2p> — exact fields **UNCONFIRMED** |
| own profile / advertiser status | `POST /v5/p2p/user/personal/info` | same SDK table — exact fields **UNCONFIRMED** |
| balance | `GET /v5/asset/transfer/query-account-coins-balance` | same SDK table |
| list ads (public-ish) | `POST /v5/p2p/item/online` | see section B |

> **There is no dedicated enable/disable (“online/offline”) endpoint in the official P2P docs.** The `docs/p2p/ad` directory contains exactly six pages: `online-ad-list` (Get Ads), `post-new-ad`, `remove-ad`, `update-list-ad`, `ad-list`, `ad-detail`. Going online is `update` with `actionType="ACTIVE"`; taking an ad down is `cancel`. A separate offline/status-toggle route is **UNCONFIRMED** (the SDK README does claim “Create, edit, delete, activate advertisements”, which matches `ACTIVE` on `update`). Ad `status` values (`10` online, `20` offline, `30` completed) are readable via `personal/list` / `info`.

### Auth scheme: API-key HMAC (no session cookie needed)

Source: <https://bybit-exchange.github.io/docs/p2p/guide>

* Access requires a **P2P-enabled API key** and **advertiser status of General Advertiser or above**; the docs state users without advertiser status cannot use the P2P API (<https://bybit-exchange.github.io/docs/p2p/guide>, help-centre article <https://www.bybit.com/en/help-center/article/Introduction-to-P2P-Open-API>).
* **Headers:**
  * `X-BAPI-API-KEY` — API key
  * `X-BAPI-TIMESTAMP` — UTC epoch **milliseconds**
  * `X-BAPI-SIGN` — signature
  * `X-BAPI-RECV-WINDOW` — validity window in ms, default `5000`
  * `X-Referer` / `Referer` — broker users only
* **Signature algorithm (HMAC-SHA256 case):**
  1. plain text = `timestamp + api_key + recv_window + queryString` for GET, or `timestamp + api_key + recv_window + jsonBodyString` for POST (the **literal** JSON body string that is sent).
  2. `HMAC_SHA256(plain_text, api_secret)`, hex-encode **lowercase** (RSA-SHA256 users: base64 of `RSA_SHA256`, and self-generated keys are RSA instead of system-generated HMAC keys).
  * Official example: `"1658384314791" + "XXXXXXXXXX" + "5000" + "category=option&symbol=BTC-29JUL22-25000-C"` → `410e0f387bafb7afd0f1722c068515e09945610124fa11774da1da857b72f30b`.
* **Timestamp recency rule:** `server_time - recv_window <= timestamp < server_time + 1000`; recommended to keep the local clock NTP-synced. Server time: `GET /v5/market/time`.
* Optional diagnostics header: `cdn-request-id` (unique per request).
* Sample scripts: <https://github.com/bybit-exchange/api-usage-examples>. **UNCONFIRMED:** whether P2P additionally requires any of the `platform`/`device-info` style browser headers used by `/x-api/fiat/...` — the P2P docs list none.
* **Sessions/CSRF:** not applicable for the V5 P2P API. The `/x-api/fiat/...` web routes use the bybit.com session cookie + the site's own CSRF/anti-bot headers; the exact cookie/CSRF names were **not** observed (403/404 only) — **UNCONFIRMED**.

### Bodies

**create — `POST /v5/p2p/item/create`** (source: post-new-ad.mdx). All scalar fields are strings unless noted.

| field | req | type | notes |
|---|---|---|---|
| `tokenId` | true | string | e.g. `USDT` |
| `currencyId` | true | string | e.g. `EUR` |
| `side` | true | string | `"0"` buy, `"1"` sell |
| `priceType` | true | string | `"0"` fixed, `"1"` variable/floating |
| `premium` | true | string | % of reference price when floating (e.g. `"130"` ⇒ 130%) |
| `price` | true | string | price in fiat per token |
| `minAmount` | true | string | min single-trade amount (fiat) |
| `maxAmount` | true | string | max single-trade amount (fiat) |
| `remark` | true | string | max length 900 |
| `tradingPreferenceSet` | true | object | see sub-fields below |
| `paymentIds` | true | array<string> | max 5; IDs from `Get User Payment`; `["-1"]` appears in examples |
| `quantity` | true | string | amount of tokens |
| `paymentPeriod` | true | string | minutes (e.g. `"15"`) |
| `itemType` | true | string | `ORIGIN` \| `BULK` |
| `tradingPreferenceSet.hasUnPostAd`, `.isKyc`, `.isEmail`, `.isMobile`, `.hasRegisterTime`, `.hasOrderFinishNumberDay30`, `.hasCompleteRateDay30`, `.hasNationalLimit` | false | string | `"0"`/`"1"` flags |
| `.registerTimeThreshold` | false | string | days |
| `.orderFinishNumberDay30` | false | string | required completed orders / 30 d |
| `.completeRateDay30` | false | string | required completion rate / 30 d |
| `.nationalLimit` | false | string | ISO-3166 alpha-3 codes |

Response: `{ ret_code, ret_msg, result: { itemId(string), securityRiskToken(string), riskTokenType(string), riskVersion(string), needSecurityRisk(boolean) }, ext_code, ext_info, ext_map, time_now }`.

**update — `POST /v5/p2p/item/update`** (source: update-list-ad.mdx). Same money fields as create (`priceType`, `premium`, `price`, `minAmount`, `maxAmount`, `remark`, `tradingPreferenceSet`, `paymentIds`, `quantity`, `paymentPeriod`) **plus**:

* `id` (string, required) — advertisement ID
* `actionType` (string, required) — `MODIFY` = change the ad; `ACTIVE` = **re-online** the ad

Response: `{ ret_code, ret_msg, result: { securityRiskToken, riskTokenType, riskVersion, needSecurityRisk }, ext_code, ext_info, time_now }` (no payload).

**remove — `POST /v5/p2p/item/cancel`**: body `{"itemId": "<string>"}`; response `result: null`, `ret_code: 0`, `ret_msg: "SUCCESS"`.

**list own ads — `POST /v5/p2p/item/personal/list`** (source: ad-list.mdx). Filters: `itemId`, `status` (`"1"` sold out, `"2"` available), `side`, `tokenId`, `page`, `size` (default 10, **max 30**), `currencyId`. Response `result.count`, `result.items[]` with `id, accountId, userId, nickName, tokenId, currencyId, side(int), priceType(int), price(string), premium(string), lastQuantity(string), quantity(string), frozenQuantity(string), executedQuantity(string), minAmount(string), maxAmount(string), remark, status(int: 10 online / 20 offline / 30 completed), createDate(string ms), payments(array<string>), hiddenReason(string, empty ⇒ visible), tradingPreferenceSet{…ints + completeRateDay30 string}, updateDate(string), feeRate(string), paymentPeriod(int minutes), itemType(string), paymentTerms[]{id(string), realName, paymentType(int), …}`, plus top-level `result.hiddenFlag` (boolean).

**ad detail — `POST /v5/p2p/item/info`**: body `{"itemId": "<string>"}`; same item schema as above (plus `version` semantics: `1` = pre-frozen funds, `2` = not pre-frozen).

### Response / error conventions

* Envelope: `ret_code` (number), `ret_msg` (string), `result` (object/null), plus `ext_code`, `ext_info`, `ext_map`, `time_now` (string seconds) on the P2P endpoints. Success is **`ret_code == 0` with `ret_msg` = `"SUCCESS"`** in every P2P sample above; the guide's generic V5 section notes `OK`/`success`/`SUCCESS`/`""` can indicate success on V5 endpoints (`retCode`/`retMsg` camelCase there — note the P2P pages use snake_case `ret_code`/`ret_msg`). Non-zero `ret_code` + descriptive `ret_msg` is the error convention — **specific numeric error codes for P2P were not enumerated in the docs I read (UNCONFIRMED; source to resolve: <https://bybit-exchange.github.io/docs/p2p/guide> error section not published for P2P, or the SDK exceptions in <https://github.com/bybit-exchange/bybit_p2p/blob/master/bybit_p2p/_exceptions.py>)**.
* Rate limits (source: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/rate-limit.mdx>): global HTTP IP limit 600 req / 5 s per IP (403 “access too frequent” ⇒ wait ≥10 min); default API limits 10 rps read / 5 rps write per UID per endpoint (`X-Bapi-Limit` header is authoritative); P2P-specific: **one ad can be modified at most 10 times within 5 minutes**.

---

## Field mapping table

| venue field path | semantics | observed type |
|---|---|---|
| `result.items[].id` (write side: `id` in update/`itemId` in cancel/info) | advertisement / item ID | string (e.g. `"1899658238346616832"`) |
| `result.items[].price` | ad price, fiat per token | **string** decimal (`"0.93"`) |
| `result.items[].minAmount` | **min single-trade amount** (fiat) | **string** (`"200"`) |
| `result.items[].maxAmount` | **max single-trade amount** (fiat) | **string** (`"9300"`) |
| `result.items[].lastQuantity` | **tradable / available token amount** | **string** (`"10000"`) |
| `result.items[].quantity` / `.frozenQuantity` / `.executedQuantity` | total / frozen-in-trade / already-executed token amount | string |
| `result.items[].payments` (array) and `paymentTerms[].paymentType`; write side `paymentIds` | payment methods / types (numeric type IDs as strings, e.g. `"14"`, `"377"`) | array<string> (resp) / array<string> (req); `paymentTerms[].paymentType` is a number |
| `result.items[].authTag` (`["GA"\|"VA"\|"BA"]`), `.authStatus` (number), `.userType` (`"ORG"`) | **merchant / advertiser flag & level** (General / Verified / Block Advertiser) | array<string> + number + string |
| `result.items[].recentOrderNum` | recent completed order count (30-day-ish counter; docs label it “Recent order number”) | **number** in the official sample (docs table wrongly says string) |
| `result.items[].recentExecuteRate` | recent execution/completion rate | **number** in the official sample (`0`); docs table says string |
| `result.items[].orderNum` / `.finishNum` | all-time order / finished-order counts | number |
| `result.items[].tradingPreferenceSet.orderFinishNumberDay30` (+`hasOrderFinishNumberDay30`) | **completed orders required in the last 30 days** (counterparty requirement), NOT the merchant's own 30-day count | number (response) / string (request) |
| `result.items[].tradingPreferenceSet.completeRateDay30` (+`hasCompleteRateDay30`) | **positive/completion rate required over 30 days**, as a percent | **string** (`"95"`, `"0"`) |
| `result.items[].side` | **side / tradeType enumeration**: `0` = buy, `1` = sell | number in responses (sample `0`); the docs’ request tables and the item tables describe it as string `"0"`/`"1"` — accept both, echo request values as strings |
| `result.items[].status` | ad status: `10` online, `20` offline, `30` completed | number |
| `result.items[].priceType` | `0` fixed rate, `1` variable rate | number (response) / string (request) |
| `result.items[].premium` | premium vs reference price for variable-rate ads (`"130"` ⇒ 130%) | string |
| `result.items[].paymentPeriod` | payment window | number (minutes) |
| `result.items[].itemType` | `ORIGIN` \| `BULK` | string |
| `result.items[].isOnline`, `.lastLogoutTime`, `.blocked`, `.hiddenReason` | maker online state / last logout / block marker / hidden reason | boolean / string(epoch) / string(`"N"`) / string |
| `result.count`, `result.hiddenFlag` (personal/list) | total count, whether any ad is hidden | int, boolean |
| (website route, **UNCONFIRMED**) `userId`, `tokenId`, `currencyId`, `payment`, `side`, `size`, `page`, `action` | request body fields attributed to `POST /x-api/fiat/otc/item/recommend/online` | unknown — body never observed |

---

## Open questions / uncertainty

1. **`POST https://www.bybit.com/x-api/fiat/otc/item/recommend/online` body + response — UNCONFIRMED.** Could not be exercised: this agent's only network capability is a GET-only URL reader. Observed here: HTTP **404 `text/html`** for GET on `/x-api/fiat/otc/item/recommend/online` (with and without query params), `/x-api/fiat/otc/item/online`, `/x-api/fiat/otc/currency/list`; architect independently observed HTTP **403 Akamai “Access Denied”** with a desktop UA (docs/research/ground-truth-probes.md). *To resolve:* run a real POST with browser headers (UA, `platform`, `device-info`, `Origin`/`Referer` for www.bybit.com, any `x-bapi-*` device/UUID headers the app sends) from a residential IP and capture the request/response — field list `userId/tokenId/currencyId/payment/side/size/page/action` remains unverified. Partial support only: Bybit's own bundle `https://www.bybit.com/static/fiat-p2p/js/main.89788ca7.js` contains the literal `recommend/online` and a `tokenId:…,currencyId` object fragment.
2. **Is `POST /v5/p2p/item/online` callable without an API key?** The official reference shows it with V5 sign headers and the P2P guide says the P2P API is advertiser-only; an anonymous call may return an auth error instead of ads. *To resolve:* one unauthenticated POST (or a POST with a key lacking P2P permission) and record `ret_code`/`ret_msg`.
3. **Exact numeric `ret_code` error values for P2P** (auth failure, `recv_window` exceeded, itemId not found, price out of band, ad-modified-too-often). Not published on the P2P pages I read. *To resolve:* Bybit P2P error-code table if it exists, or the SDK exception mapping (<https://github.com/bybit-exchange/bybit_p2p/blob/master/bybit_p2p/_exceptions.py>).
4. **Enable/disable semantics.** No `/offline` or status-toggle endpoint exists in the official six P2P ad pages; re-online = `update` + `actionType:"ACTIVE"`; take-down = `cancel`. Whether the web UI has a distinct “inactive mode” call (help-centre article “How to Put Your P2P Ads in Inactive Mode”, <https://www.bybit.com/en/help-center/article/How-to-Put-Your-P2P-Ads-in-Inactive-Mode>) that is *not* exposed in the public API is **UNCONFIRMED**.
5. **Legacy `/x-api/fiat/...` ad-management routes.** Absent from the official docs tree; **UNCONFIRMED** whether they still exist for third parties. *To resolve:* capture authenticated traffic from the merchant dashboard (`/p2p/identify/GA` → “Post Ad” form) and compare paths with `/v5/p2p/item/*`.
6. **`/v5/p2p/user/payment/list` and `/v5/p2p/user/personal/info` request/response shapes** — paths are confirmed by the official SDK README, bodies are **UNCONFIRMED** (needed to turn human payment-method names into `paymentIds` and to read advertiser status). *To resolve:* the corresponding `docs/p2p/user/*.mdx` pages (<https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/user/...>).
7. **Value ranges/constraints for `price`, `minAmount`, `maxAmount`, `quantity`, `paymentPeriod`** (per symbol/currency limits) are only partially published as `symbolInfo` fields (`currencyMinQuote`, `currencyMaxQuote`, `tokenMinQuote`, `tokenMaxQuote`, `itemDownRange`/`itemUpRange` as price-deviation percentages, allowed `paymentPeriods` in `symbolInfo.buyAd/sellAd`). Exact rejection rules are **UNCONFIRMED**.
8. **`recentOrderNum` / `recentExecuteRate` typing** is internally inconsistent between the docs tables (string) and the official sample (number) — recommend parsing both leniently; the semantics (“recent” window length) are **UNCONFIRMED**, whereas the 30-day figures live in `tradingPreferenceSet.orderFinishNumberDay30` / `.completeRateDay30` (and those are *requirements set by the merchant*, not the merchant's own statistics).

### Sources

* P2P auth/guide: <https://bybit-exchange.github.io/docs/p2p/guide>
* Get Ads: <https://bybit-exchange.github.io/docs/p2p/ad/online-ad-list> · <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/online-ad-list.mdx>
* Post Ad: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/post-new-ad.mdx>
* Update/Relist: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/update-list-ad.mdx>
* Remove Ad: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/remove-ad.mdx>
* Get My Ads: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/ad-list.mdx>
* Ad detail: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/ad/ad-detail.mdx>
* Rate limits: <https://raw.githubusercontent.com/bybit-exchange/docs/master/docs/p2p/rate-limit.mdx>
* Official SDK endpoint map: <https://github.com/bybit-exchange/bybit_p2p> · pybit P2P example: <https://github.com/bybit-exchange/pybit/blob/master/examples/p2p_example_explanatory.py>
* Help Center: <https://www.bybit.com/en/help-center/article/Introduction-to-P2P-Open-API>
* Live probes executed: `GET https://api.bybit.com/v5/market/time` → 200 JSON; `GET https://api.bybit.com/v5/order/create` → 404; `GET https://api.bybit.com/v5/p2p/item/online?tokenId=USDT&currencyId=USD&side=0` → 404 JSON; `GET https://www.bybit.com/x-api/fiat/otc/item/recommend/online[?params]` → 404 HTML; `GET https://www.bybit.com/x-api/fiat/otc/item/online?...` → 404 HTML; `GET https://www.bybit.com/x-api/fiat/otc/currency/list` → 404 HTML; `GET https://api2.bybit.com/fiat/otc/item/recommend/online` → 404 JSON; `GET https://www.bybit.com/fiat/trade/otc/buy/USDT/USD` → 200 (HTML shell, redirects to https://www.bybit.com/p2p/buy/USDT/USD, SPA — no ad data in the server-rendered HTML).
