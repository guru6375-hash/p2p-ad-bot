# Binance P2P — HTTP contract research (advertisement read + own-ad management)

Scope: Binance P2P/C2C only. Read-only investigation (no project files touched).
Method note (important for reading the evidence): my only network primitive in this session performs **HTTP GET** (verified empirically: `https://httpbin.org/anything` echoed `"method": "GET"` with `User-Agent: curl/8.0`). I cannot issue POST. Where a real POST response was required I used (a) an alternate live GET endpoint and (b) genuine archived server responses for the exact URL from the Wayback Machine, which store the original response body (`id_` raw mode).

---

## Public market-data endpoint

**Endpoint (competitor ads / order book):**

- **method:** `POST` (POST-only — GET returns 400, see below)
- **URL:** `https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search`
- **Also served on:** `https://www.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` (same handler)
- **Content-Type:** `application/json` with a JSON object body

### Live observations (exact statuses)

| Request | Observed |
|---|---|
| `GET https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` (no body, no params) | **HTTP 400** |
| `GET https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search?...full param set...` | **HTTP 400** |
| `GET https://www.binance.com/bapi/c2c/v2/friendly/c2c/adv/search?asset=USDT&fiat=USD&tradeType=SELL&page=1&rows=3` | **HTTP 400** |
| `GET https://p2p.binance.com/bapi/c2c/v2/public/c2c/config` (bogus route, reachability control) | **HTTP 404** — proves the `bapi/c2c` router on `p2p.binance.com` is reachable and answering JSON routes; the 400 is a server-side rejection of the GET/method-parameter shape, **not** geo-blocking or Cloudflare rejection |
| `GET https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list` (control) | **HTTP 200**, `code:"000000"` |
| `GET https://www.binance.com/bapi/c2c/v1/public/c2c/agent/ad-list?fiat=USD&asset=USDT&tradeType=SELL&limit=3` | **HTTP 200** (live, JSON — see "Live-verified sibling endpoint") |

I could not render the 400 response body (the fetch layer discards error bodies). Consequently **the required live POST sample is delivered from an archived server response for the exact same URL** (Wayback, `id_` raw), which is genuine payload but not fetched by me in this session:

- `https://web.archive.org/web/20260904144948id_/https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` — HTTP **200**, `application/json` (capture 2026-09-04 14:49:48 UTC)
- `https://web.archive.org/web/20210530034035id_/https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` — HTTP **200**, `application/json` (capture 2021-05-30; 638 captures of this URL exist, 2021-05-30 → 2026-09-04)

Note: the 2026 capture reflects the archiver's geo-default fiat (AED) and default filters, i.e. an effectively empty/default request. The architect's own live POST probe (recorded in `docs/research/ground-truth-probes.md`) already confirmed the same shape with `tradeType=SELL` returning the ask side with `adv.tradeType` mirrored; **nothing in my evidence contradicts that record.**

### Required headers

- `Content-Type: application/json` — required (JSON body endpoint).
- No API key, no session cookie, no CSRF token: the endpoint is fully **unauthenticated**. Neither the request-side code I found nor the responses indicate anti-bot headers are mandatory (no UUID/session/CSRF header observed in use).
- `User-Agent`: **UNCONFIRMED** whether a browser-like UA is strictly required. Browsers obviously send one; my GET attempts were rejected purely on method/params, so I have no evidence of UA-based filtering. A non-browser POST was already observed to work by the architect, which suggests no UA enforcement.

### Request body fields (types)

From shipping code at `github.com/enzonotario/dolarapi.com`, `cron/bo/binance-bo.extractor.js` (real POST body):

```js
await axios.post('https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search', {
  fiat: 'BOB',              // string (fiat unit code)
  page: 1,                  // int, 1-based
  rows: 10,                 // int, page size
  tradeType,                // string: 'BUY' | 'SELL'
  asset: 'USDT',            // string
  countries: [],            // array<string>
  proMerchantAds: false,    // bool
  shieldMerchantAds: false, // bool
  filterType: 'all',        // string
  periods: [],              // array<int|string> (payment-time buckets)
  additionalKycVerifyFilter: 0, // int
  publisherType: null,      // null | 'user' | 'merchant'
  payTypes: [],             // array<string> of trade-method identifiers, e.g. ['BANK']
  classifies: ['mass', 'profession', 'fiat_trade'], // array<string>
})
```

Independent corroboration of individual fields: `transAmount` (number/string, amount filter) and `publisherType: "merchant"` appear in the community-documented variant (StackOverflow 67793326, `https://stackoverflow.com/questions/67793326/`), and Binance's own agent-search schema documents `classifies` values as `mass / profession / block / cash`. Exact accepted enum for `classifies` on the *friendly* route (`fiat_trade` in the code above vs `cash` in current platform language) is **UNCONFIRMED**; empty/absent is the safe choice.

### Response JSON tree (exact paths, from the archived real body)

```
code                       string, "000000" on success
message                    null
messageDetail              null
data[]                     array of { adv, advertiser, ... }
  data[].adv.advNo                 string  "13928301035093368832"
  data[].adv.classify              string  "profession" | "mass" | ...
  data[].adv.tradeType             string  "SELL" | "BUY"
  data[].adv.asset                 string  "USDT"
  data[].adv.fiatUnit              string  "AED"
  data[].adv.price                 string  "3.681"        <-- STRING
  data[].adv.surplusAmount         string  "41253.59"     <-- STRING
  data[].adv.tradableQuantity      string  "41232.97"     <-- STRING
  data[].adv.initAmount            string | null
  data[].adv.amountAfterEditing    string | null
  data[].adv.minSingleTransAmount  string  "10000"       <-- STRING
  data[].adv.maxSingleTransAmount  string  "65000"       <-- STRING
  data[].adv.dynamicMaxSingleTransAmount string "65000"
  data[].adv.minSingleTransQuantity      string  "2716.65"
  data[].adv.maxSingleTransQuantity      string  "17658.24"
  data[].adv.dynamicMaxSingleTransQuantity string "17658.24"
  data[].adv.tradeMethods[]        array<object>  (payment methods)
      .payId           null | int
      .payMethodId     string ("" in observed payload)
      .payType         string  "BANK" | "BankTransferMena" | ...
      .identifier      string  "BANK"   <-- use this as the payTypes filter value
      .tradeMethodName string  "Bank Transfer"
      .tradeMethodShortName string | null
      .tradeMethodBgColor   string "#F0B90B"
      .iconUrlColor    string | null
  data[].adv.payTimeLimit          int    15
  data[].adv.takerAdditionalKycRequired int 0|1
  data[].adv.assetScale            int 2
  data[].adv.fiatScale             int 3
  data[].adv.priceScale            int 3
  data[].adv.fiatSymbol            string "د.إ"
  data[].adv.isTradable            bool true
  data[].adv.remarks               null | string
  data[].adv.autoReplyMsg          null | string
  data[].advertiser.userNo         string  "s2c07b60623f23eb891d12508a47cffd5"
  data[].advertiser.nickName       string  "BLOCKSY"
  data[].advertiser.userType       string  "merchant" | "user"
  data[].advertiser.orderCount     null (or int)
  data[].advertiser.monthOrderCount int    2166        <-- NUMBER
  data[].advertiser.monthFinishRate double 0.834      <-- NUMBER, FRACTION 0..1
  data[].advertiser.positiveRate   double 1           <-- NUMBER, FRACTION 0..1
  data[].advertiser.advConfirmTime null | int (seconds)
  data[].advertiser.userGrade      int 2|3
  data[].advertiser.userIdentity   string "BLOCK_MERCHANT" | "MASS_MERCHANT" | ""
  data[].advertiser.proMerchant    null | { merchantLogo, merchantDescription }
  data[].advertiser.badges         null | array<string> ("Block","Pro")
  data[].advertiser.vipLevel       null | int
  data[].advertiser.isBlocked      bool false
  data[].advertiser.activeTimeInSecond int 36
  data[].advertiser.merchantGroupMember bool false
  data[].privilegeDesc             null | string "Featured Ad"
  data[].privilegeType             null | int 2
```

Counts/semantics (official): `monthOrderCount` = **30-day order count**, `monthFinishRate` = **30-day completion rate**, both named in Binance's own reference table `| monthFinishRate | BigDecimal | 30-day completion rate |` and `| monthOrderCount | Integer | 30-day orders |` (`skills/binance/p2p/references/agent-sapi-api.md`, agent-sapi repo, lines ~342/~594).

### Real trimmed sample (archived response for the exact URL, capture 2026-09-04T14:49:48Z, HTTP 200)

```json
{
  "code": "000000",
  "message": null,
  "messageDetail": null,
  "data": [
    {
      "adv": {
        "advNo": "13928301035093368832",
        "classify": "profession",
        "tradeType": "SELL",
        "asset": "USDT",
        "fiatUnit": "AED",
        "price": "3.681",
        "initAmount": null,
        "surplusAmount": "41253.59",
        "tradableQuantity": "41232.97",
        "maxSingleTransAmount": "65000",
        "minSingleTransAmount": "10000",
        "payTimeLimit": 15,
        "tradeMethods": [
          { "payId": null, "payMethodId": "", "payType": "BANK", "identifier": "BANK",
            "tradeMethodName": "Bank Transfer", "tradeMethodBgColor": "#F0B90B" },
          { "payId": null, "payMethodId": "", "payType": "BankTransferMena", "identifier": "BankTransferMena",
            "tradeMethodName": "Bank Transfer (Middle East)", "tradeMethodBgColor": "#F0B90B" }
        ],
        "takerAdditionalKycRequired": 1,
        "assetScale": 2, "fiatScale": 3, "priceScale": 3, "fiatSymbol": "د.إ",
        "isTradable": true,
        "dynamicMaxSingleTransAmount": "65000",
        "minSingleTransQuantity": "2716.65", "maxSingleTransQuantity": "17658.24",
        "dynamicMaxSingleTransQuantity": "17658.24"
      },
      "advertiser": {
        "userNo": "s2c07b60623f23eb891d12508a47cffd5",
        "nickName": "BLOCKSY",
        "orderCount": null,
        "monthOrderCount": 2166,
        "monthFinishRate": 1,
        "positiveRate": 1,
        "userType": "merchant",
        "userGrade": 3,
        "userIdentity": "BLOCK_MERCHANT",
        "badges": ["Block", "Pro"],
        "vipLevel": 3,
        "isBlocked": false,
        "activeTimeInSecond": 36,
        "merchantGroupMember": false
      },
      "privilegeDesc": "Featured Ad",
      "privilegeType": 2,
      "privilegeTypeAdTotalCount": 1
    },
    {
      "adv": { "advNo": "12929187549246205952", "classify": "mass", "tradeType": "SELL", "asset": "USDT",
        "fiatUnit": "AED", "price": "3.676", "surplusAmount": "2375.51", "tradableQuantity": "2373.13",
        "maxSingleTransAmount": "10000", "minSingleTransAmount": "7000", "payTimeLimit": 15,
        "tradeMethods": [ { "payType": "BANK", "identifier": "BANK", "tradeMethodName": "Bank Transfer" } ] },
      "advertiser": { "userNo": "se4e6c3e334f237c6b7cdd124b49b377c", "nickName": "maher show",
        "orderCount": null, "monthOrderCount": 5, "monthFinishRate": 0.834, "positiveRate": 1,
        "userType": "user", "userGrade": 2, "userIdentity": "", "merchantGroupMember": false }
    }
  ]
}
```

### Live-verified sibling endpoint (GET, no auth) — useful as a fallback / cross-check

Binance's own skill documentation exposes a **GET** public agent family on `www.binance.com`; I fetched it live:

- `GET https://www.binance.com/bapi/c2c/v1/public/c2c/agent/ad-list?fiat=USD&asset=USDT&tradeType=SELL&limit=3` → **HTTP 200**
- `GET https://www.binance.com/bapi/c2c/v1/public/c2c/agent/quote-price?fiat=USD&asset=USDT&tradeType=BUY` (documented; not fetched)
- `GET https://www.binance.com/bapi/c2c/v1/public/c2c/agent/trade-methods?fiat=CNY` (documented; not fetched)

Live trimmed body (2026-09-24, `code:"000000"`, `success:true`):

```json
{"code":"000000","message":null,"messageDetail":null,"data":{"items":[
 {"adNo":"12918177341462491136","price":1.1,"fiat":"USD","fiatSymbol":"$","fiatScale":2,
  "asset":"USDT","assetScale":2,"priceScale":3,"minTransAmount":13.63,"maxTransAmount":4545.45,
  "tradableAmount":2799.67,"payTimeLimit":15,
  "tradeMethods":["stcpay","GPay","IMPS","PhonePe","EasypaisaPK"],
  "advertiser":{"nickName":"princessFatimaRani","userType":"user","monthOrderCount":15,
   "monthFinishRate":0.715,"positiveRate":0.9375,"merchantGroupMember":false}},
 ...]},"success":true}
```

This is the strongest **live, first-party, unauthenticated** proof of the rate-field semantics: `monthFinishRate: 0.715` and `positiveRate: 0.9375` are **fractions in [0,1], JSON numbers** (not percentages, not strings). Note this agent shape uses different names (`adNo`, `price` as a **number** here, `minTransAmount`, `maxTransAmount`, `tradableAmount`, `tradeMethods` as bare strings) — do not conflate it with the friendly shape.

### `payTypes` / `payMethods` — correction

The task asked for `adv.payTypes` / `adv.payMethods` in the **friendly** response. In **both** archived friendly bodies (2021 and 2026) the payment methods are carried in **`data[].adv.tradeMethods[]`** (objects with `payType`, `identifier`, `tradeMethodName`, …); there is **no `adv.payTypes` and no `adv.payMethods` key** in the observed friendly payload. `payTypes` exists only as a **request** filter field (array of identifier strings). If a `payMethods` key exists in another variant (e.g. a mobile/app response), that is **UNCONFIRMED**. Practical rule: read `adv.tradeMethods[].identifier` (filter values) and `adv.tradeMethods[].tradeMethodName` (display).

---

## Private advertisement API

There are **two separate private paths**. Binance's *documented, supported* one is an **API-key/HMAC** product ("C2C Agent SAPI"); the one the P2P **web** page uses is an undocumented **session-cookie** API.

### A. Official API-key product — C2C Agent SAPI (RECOMMENDED, fully documented by Binance)

- **Base URL:** `https://api.binance.com`
- **Docs (Binance's own repo):** `https://github.com/binance/binance-skills-hub/blob/main/skills/binance/p2p/references/agent-sapi-api.md`; overview in `.../skills/binance/p2p/SKILL.md`; signing in `.../references/authentication.md`
- **Product name:** Binance P2P skill reference calls it *Agent SAPI API*; the pre-existing public SAPI endpoints live under the same `/sapi/v1/c2c/...` namespace (e.g. `GET /sapi/v1/c2c/orderMatch/listUserOrderHistory`). It requires **"Enable Reading"** for queries and **write/merchant permission** for ad writes.

**Exact paths**

| Operation | Method | Path |
|---|---|---|
| Create advertisement | POST | `/sapi/v1/c2c/agent/ads/post` |
| Update advertisement (price/amounts/everything) | POST | `/sapi/v1/c2c/agent/ads/update` |
| Enable / disable / close (batch) | POST | `/sapi/v1/c2c/agent/ads/updateStatus` |
| List my own advertisements | POST | `/sapi/v1/c2c/agent/ads/listWithPagination` |
| Get one of my ads by number | POST | `/sapi/v1/c2c/agent/ads/getDetailByNo?advNo={advNo}` |
| Search market ads (includes my view of the market) | POST | `/sapi/v1/c2c/agent/ads/search` |
| Reference price | POST | `/sapi/v1/c2c/agent/ads/getReferencePrice` |
| My payment methods (needed for SELL ads) | GET | `/sapi/v1/c2c/agent/ads/getPayMethodByUserId` |
| Publishable ad categories | GET | `/sapi/v1/c2c/agent/ads/getAvailableAdsCategory` |
| All system trade methods (needed for BUY ads) | POST | `/sapi/v1/c2c/agent/ads/listAllTradeMethods` |
| Merchant profile + their buy/sell ads | GET | `/sapi/v1/c2c/agent/merchant/getAdDetails?merchantNo={merchantNo}` |

**Auth scheme (HMAC API key — this is the scheme that exists; there is no merchant API variant that bypasses it)**

- Headers: `X-MBX-APIKEY: <key>` and (per Binance's own reference) `User-Agent: binance-wallet/1.0.0 (Skill)`.
- `timestamp` (ms, Unix epoch) **required**; `recvWindow` optional, **default 60000 ms for P2P endpoints** (max 60000 on standard SAPI).
- Signature: **`signature` = hex HMAC-SHA256 of the percent-encoded (RFC 3986) query string, using the secret key**, appended as a query param.
- **Critical quirk (Binance's own wording): "DO NOT sort parameters" for these SAPI endpoints** — unlike the standard Binance REST API. Keep insertion order when building the string to sign.
- Signature case-sensitivity: HMAC signatures are case-insensitive (standard SAPI doc).
- For `POST` endpoints the docs' curl examples put `timestamp`+`signature` in the **query string** and the payload as a JSON body.
- Errors: standard Binance SAPI error convention `{"code": -2015| -1021| -1022| -9000| -1002, "msg": "..."}`; the skill lists `-2015` invalid key, `-1022` signature failed, `-1021` timestamp invalid.

**Create (`ads/post`) body** — types as documented:

```
classify                 string   required, e.g. "mass" (mass|profession|block|cash)
tradeType                string   required, BUT numeric enum here: "0"=BUY, "1"=SELL
asset                    string   required (BTC|ETH|USDT|BNB|...)
fiatUnit                 string   required (CNY|USD|...)
priceType                int      required: 1=Fixed, 2=Floating
price                    number   required if priceType=1
priceFloatingRatio       number   required if priceType=2 (percent)
rateFloatingRatio        number   optional
initAmount               number   required (total crypto amount)
maxSingleTransAmount     number   required (fiat per order)
minSingleTransAmount     number   required (fiat per order)
buyerKycLimit            int      required 0|1
buyerRegDaysLimit        int      optional
buyerBtcPositionLimit    number   optional
remarks                  string   optional, max 1000 chars (no crypto-related words)
autoReplyMsg             string   optional, max 1000 chars
onlineNow                bool     optional (default true)
payTimeLimit             int      optional (minutes, default 15)
tradeMethods             array    required: [{ payId (long, SELL ads), payType (string), identifier (string, BUY ads) }]
takerAdditionalKycRequired int    optional 0|1
launchCountry            array<string> optional
```
Response: `CommonRet<String>` where `data` = the new `advNo`.

**Update (`ads/update`) — full-object update**

```
advNo string required; tradeType, asset, fiatUnit, priceType, priceFloatingRatio, rateFloatingRatio,
price, initAmount, maxSingleTransAmount, minSingleTransAmount, buyerKycLimit, buyerRegDaysLimit,
buyerBtcPositionLimit, remarks, autoReplyMsg, payTimeLimit, tradeMethods[], advStatus, 
takerAdditionalKycRequired, launchCountry -- all optional individually
```
- Binance explicitly warns: *"the downstream service validates the complete ad object… Sending only `advNo` + one field (e.g. price) will result in error `-9000`"*. Workflow: `getDetailByNo` → merge changes into the full object → POST it to `ads/update`.
- To change price on a fixed-price ad: set `priceType: 1` + `price`. To change limits: `minSingleTransAmount` / `maxSingleTransAmount`. To change quantity: `initAmount`.
- Response: `CommonRet<Boolean>` (`data: true` on success).

**Enable / disable / close (`ads/updateStatus`)**

```
advNos    array<string> required (min 1)
advStatus int           required: 1=Online, 3=Offline, 4=Closed
```
Response `AgentAdUpdateStatusResp`: `{ status: bool, failList: [{ advNo, errorCode, errorMessage }] }`.
Note the documented enum for status write is **1=Online, 3=Offline, 4=Closed**, while the skill's display table lists `1 Online, 2 Offline, 4 Closed` — the **`2` vs `3` for Offline is inconsistent between Binance's own two pages and is UNCONFIRMED** (send `1` to enable and `3` to disable, as the API reference states).

**List my ads (`ads/listWithPagination`)**

```
page int optional (default 1)
rows int optional (default 20)
```
Response: paginated list of the same `AgentAdDetailResp` shape returned by `getDetailByNo`:

```
advNo string, classify string, tradeType string ("BUY"/"SELL" — note: STRING here, unlike post/update),
asset string, fiatUnit string, advStatus int (1=Online,3=Offline,4=Closed),
priceType int, priceFloatingRatio number, price number, initAmount number, surplusAmount number,
tradableQuantity number, maxSingleTransAmount number, minSingleTransAmount number,
payTimeLimit int, remarks string, autoReplyMsg string, createTime date,
tradeMethods[{identifier, tradeMethodName, iconUrlColor}], commissionRate number,
buyerKycLimit int, buyerRegDaysLimit int, buyerBtcPositionLimit number, takerAdditionalKycRequired int
```
Response wrapper (all agent endpoints): `{ "code": "000000", "message": null, "data": ..., "success": true }`; paginated: `{ code, data: [...], total: 100, success: true }`. Error cases: `-1002` when querying another user's ad; `-9000` malformed full-object update; business errors surfaced as `Permission denied`, `FIAT_ASSET_ILLEGAL`, `ILLEGAL_PARAMETERS`.

### B. Web-session private API (what p2p.binance.com's own UI calls)

- **Base URL:** `https://p2p.binance.com` (web app also hosted at `https://c2c.binance.com`)
- **Path family:** `https://p2p.binance.com/bapi/c2c/v2/private/c2c/adv/<action>` (v2) and `.../bapi/c2c/v1/private/c2c/<resource>/<action>` (v1).
- **Confirmed single instance** (Wayback CDX, status 401 = session required):
  `20241104101257 https://p2p.binance.com/bapi/c2c/v2/private/c2c/adv/publish-check 401`
  source: `http://web.archive.org/cdx/search/cdx?url=p2p.binance.com/bapi/c2c/v2/private/c2c/adv*&fl=timestamp,original,statuscode&collapse=urlkey`
- Other confirmed 401 (unauthenticated) private C2C routes on the same host: `.../bapi/c2c/v1/private/c2c/asset/balance?stamp=...`, `.../v1/private/c2c/user/base-detail`, `.../v1/private/c2c/user/profile`, `.../v2/private/c2c/order-match/order-list`, `.../v2/private/c2c/pay-method/user-paymethods`, `.../v2/private/c2c/trade-method/detail/{identifier}`, `.../v1/private/binance-chat/chat-list`.
- **Auth:** logged-in **session cookies** (`p20t`/`BNC`/`bnc-uuid`-style Binance cookies) — every private route returns **HTTP 401** without them, observed in the archive for the routes above and derivable for the rest. No API key works on this family.
- **CSRF: UNCONFIRMED.** The task premise mentions an `X-CSRF-TOKEN` header obtained from a specific endpoint. I could **not** verify any CSRF token endpoint or header name for the C2C/P2P bapi family: a CDX sweep of `www.binance.com/bapi/accounts/v1/private/*` (69 archived private account routes) shows no `csrf`/`token` route, and code searches for `"x-csrf-token"` + binance returned zero hits (Sourcegraph: `https://sourcegraph.com/search?q=context:global+%22x-csrf-token%22+binance`). What would resolve it: opening the P2P ad-create page in a logged-in browser and recording the request headers of the `POST .../v2/private/c2c/adv/publish` call (or reading the Next.js bundle that builds the request).
- **Exact create / update / enable-disable / list paths on this family: UNCONFIRMED.** Only `adv/publish-check` is evidenced. Most likely names (`adv/publish`, `adv/update`, `adv/update-status` or `adv/status`, `adv/list`) are **guesses and must not be treated as fact**. What would resolve it: the browser's DevTools network log while creating/updating/taking an ad offline and pausing it, plus the P2P page's JS bundle (`https://p2p.binance.com` static assets) which contains the route strings.
- Ad deep-link (public, documented by Binance's own skill): `https://c2c.binance.com/en/adv?code={advNo}`.

**Recommendation for the bot:** implement own-ad management on **A (C2C Agent SAPI, API-key HMAC)** — it is officially documented, deterministic, and requires only key management; use family **B only for read-only public data**, and avoid it for writes (undocumented, session/CSRF-bound, breaks silently).

---

## Field mapping table

Types are **as observed** in the payloads cited above. "friendly" = `p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search`; "agent-SAPI" = `api.binance.com/sapi/v1/c2c/agent/*`; "agent-public" = `www.binance.com/bapi/c2c/v1/public/c2c/agent/*` (live-fetched).

| venue field path | semantics | observed type |
|---|---|---|
| friendly `data[].adv.advNo` | advertisement id | string (e.g. `"13928301035093368832"`) |
| agent-SAPI `data.advNo` (getDetailByNo/listWithPagination/post response `data`) | advertisement id | string |
| agent-public `data.items[].adNo` | advertisement id | string |
| friendly `data[].adv.price` | unit price of 1 crypto in fiat | **string** (`"3.681"`) |
| agent-SAPI `data.price` | unit price | number (BigDecimal) |
| agent-public `data.items[].price` | unit price | **number** (`1.1`) |
| friendly `data[].adv.minSingleTransAmount` | min fiat per single trade | **string** (`"10000"`) |
| friendly `data[].adv.maxSingleTransAmount` | max fiat per single trade | **string** (`"65000"`) |
| friendly `data[].adv.dynamicMaxSingleTransAmount` | effective max fiat (after holdings/limits) | string |
| agent-SAPI `data.minSingleTransAmount` / `data.maxSingleTransAmount` | min / max fiat per order | number |
| agent-public `data.items[].minTransAmount` / `.maxTransAmount` | min / max fiat per order | number |
| friendly `data[].adv.tradableQuantity` | tradable crypto quantity (what you can actually take now) | **string** (`"41232.97"`) |
| friendly `data[].adv.surplusAmount` | remaining crypto amount on the ad | **string** (`"41253.59"`) |
| friendly `data[].adv.initAmount` / `.amountAfterEditing` | original / post-edit total crypto amount | string or null |
| agent-SAPI `data.tradableQuantity` / `data.surplusAmount` / `data.initAmount` (my own ads) | tradable / remaining / initial crypto | number |
| agent-public `data.items[].tradableAmount` | remaining crypto | number |
| friendly `data[].adv.asset` | crypto asset | string (`"USDT"`) |
| friendly `data[].adv.fiatUnit` | fiat code | string (`"AED"`, `"CNY"`, `"USD"`) |
| agent-public `data.items[].fiat` | fiat code | string |
| friendly `data[].adv.tradeType` | side, mirrored to the queried side | string `"SELL"` \| `"BUY"` |
| agent-SAPI (write: post/update) `tradeType` | side | **string containing a numeric enum**: `"0"`=BUY, `"1"`=SELL |
| agent-SAPI (read: getDetailByNo/`ads/search`) `tradeType` | side | string `"BUY"` \| `"SELL"` |
| agent-SAPI `listOrders` `tradeType` | side | documented as `0=BUY, 1=SELL` (int/string ambiguous) — UNCONFIRMED exact JSON type |
| friendly `data[].adv.tradeMethods[]` | payment methods on the ad | array<object> |
| → `.identifier` | payment method identifier (use as `payTypes` filter value) | string (`"BANK"`, `"Zelle"`, `"BankTransferMena"`) |
| → `.payType` | payment type | string (`"BANK"`) |
| → `.tradeMethodName` | display name | string (`"Bank Transfer"`) |
| agent-SAPI `tradeMethods[].identifier` / `.tradeMethodName` / `.payId` | payment methods of my ad | string / string / long (payId needed for SELL writes) |
| agent-public `data.items[].tradeMethods[]` | payment methods | array<string> (bare identifiers) |
| friendly `data[].advertiser.userType` | merchant flag (`user` vs `merchant`) | string (`"merchant"` \| `"user"`) |
| friendly `data[].advertiser.userIdentity` | merchant grade (`BLOCK_MERCHANT`, `MASS_MERCHANT`, `""`) | string |
| friendly `data[].advertiser.merchantGroupMember` | member of merchant group | bool |
| agent-SAPI `userType` (merchant profile / advertiser) | merchant flag | string `user` \| `merchant` |
| agent-public `data.items[].advertiser.userType` | merchant flag | string |
| friendly `data[].advertiser.monthOrderCount` | **30-day order count** | **number** (`2166`, `5`) |
| agent-SAPI `monthOrderCount` | 30-day orders | integer (documented) |
| agent-public `data.items[].advertiser.monthOrderCount` | 30-day orders | number (`15`) |
| friendly `data[].advertiser.monthFinishRate` | **30-day completion rate** | **number, FRACTION 0..1** (`0.834`, `1`) |
| agent-SAPI `monthFinishRate` | 30-day completion rate | number (BigDecimal) |
| agent-public `.advertiser.monthFinishRate` | 30-day completion rate | number, fraction (`0.715`) |
| friendly `data[].advertiser.positiveRate` | positive-feedback rate | **number, FRACTION 0..1** (`1`, `0.9375` on the live agent-public sample) |
| agent-public `.advertiser.positiveRate` | positive rate | number, fraction (`0.9375`) |
| friendly `data[].advertiser.orderCount` | lifetime order count | null or integer |
| friendly `data[].advertiser.nickName` | display name (not masked) | string |
| friendly `data[].advertiser.userNo` | user number | string (`"s2c07b60623f23eb891d12508a47cffd5"`) |
| agent-SAPI `userNo` (advertiser block) | user number | string |
| friendly `data[].adv.payTimeLimit` | payment window (minutes) | number (`15`) |
| friendly `data[].adv.classify` | ad category | string `mass` \| `profession` \| `block` \| `cash` (`"fiat_trade"` also seen in third-party code) |
| friendly `data[].adv.assetScale` / `.fiatScale` / `.priceScale` | decimal scales | number |
| friendly `data[].adv.fiatSymbol` | fiat symbol | string (`"د.إ"`, `"￥"`) |
| friendly `data[].adv.isTradable` | currently takeable | bool |
| friendly `data[].adv.takerAdditionalKycRequired` | extra verification gate | number 0\|1 |
| friendly `data[].advertiser.advConfirmTime` | average release time (s) | null or number |
| friendly `data[].advertiser.activeTimeInSecond` | seconds since last active | number |
| friendly `data[].advertiser.userGrade` / `.vipLevel` / `.badges` / `.proMerchant` | badges/grade metadata | number / null-int / null-array<string> / null-object |
| friendly `data[].privilegeDesc` / `.privilegeType` | featured-ad flag | string/null / number/null |
| friendly `data[].adv.advStatus` (public search) | always `null` in observed payloads | null |
| agent-SAPI `advStatus` (own ads) | `1`=Online, `3`=Offline, `4`=Closed (write API) — `2`=Offline appears in the skill's display table | integer |
| friendly `code` / `message` / `messageDetail` | envelope | string `"000000"` / null / null |
| agent-SAPI `code` / `message` / `data` / `success` | envelope | string / null / object / bool |
| agent-public `code` / `data` / `success` | envelope | string / object / bool |

**Side semantics (official, from Binance's own skill doc, "tradeType mapping"):** user **buys crypto** → `tradeType=BUY`; user **sells crypto** → `tradeType=SELL`. Therefore, to read the **ask side** (ads where merchants sell crypto, i.e. what a buyer pays), send `tradeType=SELL`. For the **write** APIs (`ads/post`, `ads/update`) the numeric form is used: `"0"`=BUY, `"1"`=SELL.

---

## Open questions / uncertainty

1. **Live POST of the friendly search endpoint by me.** Not possible with this session's GET-only fetch primitive. Statuses I did observe: 400 (GET, both hosts), 404 (bogus control route). Responses for the real URL come from Wayback captures 2021-05-30 and 2026-09-04 (both HTTP 200 `application/json`, `id_` raw). To resolve: a POST-capable client (`curl -X POST ... -H 'content-type: application/json' -d '{...}'`).
   - Tried: `https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` (GET, with and without params), `https://www.binance.com/bapi/c2c/v2/friendly/c2c/adv/search?asset=USDT&fiat=USD&tradeType=SELL&page=1&rows=3`.
2. **Mandatory anti-bot headers.** UNCONFIRMED that any UUID/session/CSRF header is needed — none appears in code or docs; the endpoint is unauthenticated. To resolve: capture the browser request headers on the P2P page.
3. **`adv.payTypes` / `adv.payMethods`.** Not present in the observed friendly payloads (only `adv.tradeMethods[]`); `payTypes` exists as a request filter. UNCONFIRMED whether another API variant returns `payMethods`. To resolve: inspect a mobile/app response or a `GET` agent variant.
4. **Exact enum accepted in the friendly request's `classifies`.** Third-party code sends `fiat_trade`; Binance's current docs list `mass, profession, block, cash`. UNCONFIRMED which the friendly route accepts. To resolve: live POST with each value.
5. **Web-session private ad endpoints (create/update/enable-disable/list).** UNCONFIRMED paths; only `/bapi/c2c/v2/private/c2c/adv/publish-check` (401) is evidenced (`http://web.archive.org/cdx/search/cdx?url=p2p.binance.com/bapi/c2c/v2/private/c2c/adv*`). The names I would expect are not evidence and must not be shipped as fact. To resolve: logged-in DevTools capture of creating, price-updating, pausing and re-enabling an ad, plus the P2P page JS bundle.
6. **CSRF mechanism for the web private API.** UNCONFIRMED: no token endpoint found in a CDX sweep of `www.binance.com/bapi/accounts/v1/private/*` and no code evidence for `X-CSRF-TOKEN` on Binance (`https://sourcegraph.com/search?q=context:global+%22x-csrf-token%22+binance` → 0 hits; `https://sourcegraph.com/search?q=context:global+bapi/c2c/v1/private` → 0 hits; `https://grep.app/api/search?q=bapi%2Fc2c%2Fv1%2Fprivate` → HTTP 429 rate-limited on every attempt). To resolve: browser header capture.
7. **`advStatus` = 2 vs 3 for Offline.** Binance's API reference says write `3` for Offline; the same repo's skill table says `2`=Offline. Pick `3` for writes; verify against a live response. UNCONFIRMED.
8. **`monthFinishRate` display convention.** The live values are fractions (`0.715`), yet Binance's skill renders `{monthFinishRate}%` — that page is multiplying for display. Treat stored values as fractions; do not multiply a second time. (Confirmed live: agent-public `monthFinishRate: 0.715`, `positiveRate: 0.9375`.)
9. **Rate limits for the public friendly endpoint and for the agent SAPI.** Not documented for these routes; standard SAPI IP/UID limits apply to `/sapi/*` (12000/min IP or 180000/min UID), and a 429/418 escalation exists. No P2P-specific numbers found — UNCONFIRMED.
10. **`recvWindow` default 60000 ms for P2P.** Stated by Binance's skill `authentication.md`; the generic SAPI doc says 5000 ms / 60000 max. Use ≤60000 and treat 60000 as safe — the P2P-specific claim is only single-sourced (Binance's own repo), so: UNCONFIRMED against the main API doc.

### Source URLs

- Friendly search: `https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` (POST); archived real bodies `https://web.archive.org/web/20260904144948id_/https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search` and `https://web.archive.org/web/20210530034035id_/https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search`
- Live public GET sibling: `https://www.binance.com/bapi/c2c/v1/public/c2c/agent/ad-list?fiat=USD&asset=USDT&tradeType=SELL&limit=3` (HTTP 200)
- Request-body code: `https://raw.githubusercontent.com/enzonotario/dolarapi.com/98bb6a78817d248299885289b0cdf478b7a11fde/cron/bo/binance-bo.extractor.js`
- Private API reference (Binance): `https://github.com/binance/binance-skills-hub/blob/main/skills/binance/p2p/references/agent-sapi-api.md`, `.../skills/binance/p2p/SKILL.md`, `.../skills/binance/p2p/references/authentication.md`
- Generic SAPI rules/error codes: `https://developers.binance.com/en/docs/products/c2c/general-info`
- Private web route evidence: `http://web.archive.org/cdx/search/cdx?url=p2p.binance.com/bapi/c2c/v2/private/c2c/adv*` (401 `publish-check`), `http://web.archive.org/cdx/search/cdx?url=p2p.binance.com/bapi/c2c*` (many 401 private routes), `http://web.archive.org/cdx/search/cdx?url=www.binance.com/bapi/c2c*`
