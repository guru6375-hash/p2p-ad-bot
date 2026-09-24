# OKX P2P — venue research (read-only, live-verified)

All probes were made anonymously from a shared datacenter IP with the generic `read` HTTP client (no browser fingerprint, no cookies, no JS). Where a probe "succeeded" my tool reports no HTTP error; OKX surfaces 4xx as tool failures with the status echoed, so the statuses quoted below are observed exactly as returned.

## Public market-data endpoint

- **Method / URL**: `GET https://www.okx.com/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin`
- **Required headers**: none. No `User-Agent` requirement, no cookie, no CSRF token, no `x-csrf-token`, no `x-utc`, no anti-bot/UUID header. Verified: the request below returned parsed JSON from a datacenter IP with a plain client. (OKX web front-end likely sends extra headers; they are not required.)
- **Query params (all strings; all observed accepted verbatim)**:

| param | observed value(s) | notes |
| --- | --- | --- |
| `paymentMethod` | `all` | filter by payment-method name/code |
| `side` | `sell` \| `buy` | selects which array is populated (see below) |
| `userType` | `all` (also tried `merchant`) | **no filtering effect observed** — architect's live probe agrees; treat as cosmetic |
| `sortType` | `price_asc` \| `price_desc` | |
| `limit` | `100` | |
| `cryptoCurrency` | `USDT` | |
| `fiatCurrency` | `UAH` \| `PLN` | |
| `currentPage` | `1` | |
| `numberPerPage` | `5` | page size actually honored (5 rows returned) |
| `t` | epoch-ms (e.g. `1758700000000`) | cache-buster |

- **Response tree** (identical shape for UAH and PLN, `side=sell` and `side=buy`):

```
code            int      0
msg             string   ""
detailMsg       string   ""
error_code      string   "0"     <- string, NOT int
error_message   string   ""
requestId       string   "3121002658036220002"
data.total      int      100      (UAH sell: 100; PLN buy: 88)
data.sell       array    rows when side=sell, [] when side=buy
data.buy        array    rows when side=buy,  [] when side=sell
```

  So **there is no separate endpoint per side**: `side=sell` returns the rows in `data.sell`, `side=buy` returns them in `data.buy`; the other array is always `[]`.

- **Observed HTTP status**: `200` (fetch succeeded, `Content-Type: application/json`, body parsed as JSON) for both UAH and PLN, both sides.
- **Field paths in each row** (types as observed, all money/rate values are JSON **strings**):

| path | observed value | JSON type |
| --- | --- | --- |
| `id` | `"260924231439195"` | string |
| `price` | `"46.96"` / `"3.97"` | string |
| `availableAmount` | `"5399.28"` | string |
| `quoteMinAmountPerOrder` | `"3999.00"` | string |
| `quoteMaxAmountPerOrder` | `"253550.18"` | string |
| `paymentMethods` | `["Oschad bank (CARD)", …]` | array of string |
| `creatorType` | `"diamond"` \| `"certified"` \| `"common"` | string |
| `merchantId` | `"a5e7627aea"` (empty `""` for non-merchants) | string |
| `userType` | `"all"` \| `"common"` | string |
| `completedOrderQuantity` | `680` | number (int) |
| `completedRate` | `"0.9483"` | string (fraction, 4dp) |
| `posReviewPercentage` | `"-1"` | string (`-1` = not available) |
| `nickName` | `"Anticorupcioner"` (masked `"ser***@gmail.com"` for some) | string |
| `publicUserId` | `"8be31f0944"` | string |
| `side` | `"sell"` \| `"buy"` | string |
| `baseCurrency` / `quoteCurrency` | `"usdt"` / `"uah"` | lowercase string |
| `quoteScale` / `quoteSymbol` | `2` / `"₴"` | number / string |
| `badgeInfo.badgeList[].title` | `"Diamond Merchant"` (badgeId `-1000`), `"Super Merchant"` (`-1001`) | string/int |
| `boostType` | `"PAID"` \| `null` | string or null |
| `paymentTimeoutMinutes` | `15` | number |
| `cancelledOrderQuantity` | `37` | number |
| `verifiedOnlyKycLevel` etc. | `minKycLevel:1`, `minSellOrders:0`, `minCompletedOrderQuantity:0`, `minCompletionRate:"0.0"`, `whitelistedCountries:["ALL_COUNTRIES"]` | mixed |

- **Real trimmed sample (UAH, side=sell)**:

```json
{
  "code": 0,
  "data": {
    "buy": [],
    "sell": [
      {
        "availableAmount": "5399.28",
        "completedOrderQuantity": 680,
        "completedRate": "0.9483",
        "creatorType": "diamond",
        "id": "260924231439195",
        "merchantId": "a5e7627aea",
        "nickName": "Anticorupcioner",
        "paymentMethods": ["Oschad bank (CARD)","PUMB (CARD)","Monobank (Card)","PrivatBank (CARD)"],
        "paymentTimeoutMinutes": 15,
        "posReviewPercentage": "-1",
        "price": "46.96",
        "publicUserId": "8be31f0944",
        "quoteCurrency": "uah",
        "quoteMaxAmountPerOrder": "253550.18",
        "quoteMinAmountPerOrder": "3999.00",
        "quoteScale": 2,
        "quoteSymbol": "₴",
        "side": "sell",
        "userType": "all",
        "badgeInfo": {"badgeList": [{"badgeId": -1000, "title": "Diamond Merchant", "type": 1}]}
      }
    ],
    "total": 100
  },
  "detailMsg": "",
  "error_code": "0",
  "error_message": "",
  "msg": "",
  "requestId": "3121002658036220002"
}
```

- **Real trimmed sample (PLN, side=buy, sortType=price_desc)** — note the mirror-array behaviour:

```json
{
  "code": 0,
  "data": {
    "buy": [
      {
        "availableAmount": "94558.72",
        "completedOrderQuantity": 115,
        "completedRate": "0.9426",
        "creatorType": "certified",
        "id": "260924234850818",
        "merchantId": "01fdc4bf15",
        "nickName": "arxfatalis",
        "paymentMethods": ["bank","Bank Pekao","PKO Bank","BLIK","Santander"],
        "price": "3.79",
        "quoteMaxAmountPerOrder": "8000.00",
        "quoteMinAmountPerOrder": "300.00",
        "quoteSymbol": "zł",
        "side": "buy",
        "userType": "common"
      }
    ],
    "sell": [],
    "total": 88
  },
  "error_code": "0",
  "msg": "",
  "requestId": "3130802658075430012"
}
```

- **Note on "sentinel" values**: `avgCompletedTime: -1`, `avgPaymentTime: -1`, `maxCompletedOrderQuantity: -1`, `minTradeVolume: 0`, `uniqueUsersTradedWith: 0`, `followerCount: 0` were constant across both fiats and both sides; do not interpret them as data (they looked unpopulated / `-1` = N/A) — UNCONFIRMED which of them are ever populated.

## Private advertisement API

### Summary of what exists (all verified live by HTTP probe)

| Surface | Endpoint | Observed unauth response | Interpretation |
| --- | --- | --- | --- |
| API-key v5 P2P | `POST /api/v5/p2p/ad/create` | `405` (application/json) to GET | path exists, POST-only |
| API-key v5 P2P | `POST /api/v5/p2p/ad/update` | `405` (application/json) to GET | path exists, POST-only |
| API-key v5 P2P | `GET /api/v5/p2p/order/list` | `401` (application/json) | path exists, API-key required |
| API-key v5 P2P | `GET /api/v5/p2p/ad/{query-ads,query,query-list,list,detail,amend,enable,disable,update-sell-ad,update-buy-ad,status,my-ads,get-list,delete}` | `404` | **not** the real names (see open questions) |
| v5 control | `GET /api/v5/account/balance` | `401`, body `{"msg":"Request header OK-ACCESS-KEY can not be empty.","code":"50103"}` | auth-error convention |
| v5 control | `GET /api/v5/zzz/nonexistent` | `404` | 404 = unknown route |
| Web-private | `GET /v3/c2c/tradingOrders/getMyAds` | `403` (application/json) | path exists; session auth required |
| Web-private control | `GET /v3/c2c/advertisement/getMyAds` | `404` | 404 = wrong namespace, so the 403 above is meaningful |

**Therefore: yes, OKX exposes an official, API-key-authenticated P2P API, product family `/api/v5/p2p/*`, and it is the correct integration target for creating/updating the bot's own advertisements.** It is not open to everyone: it is reserved for **Super and Diamond P2P merchants** who apply and are whitelisted by OKX's Merchant Management Team — official announcement: https://www.okx.com/help/announcing-the-p2p-api-for-super-and-diamond-merchants-trade-smarter ("Eligibility: You're a Super or Diamond Merchant"; apply at `https://www.okx.com/p2p/api`, which returned 404 from the EEA/US edge I was routed to — the page exists globally per search engines). Terms: https://www.okx.com/help/p2p-api-user-agreement (no fee at signing, explicit whitelisting requirement, clause 4.2 bans unauthorised use of another user's credentials or probing for vulnerabilities; reasonable rate limits required).

### Base URL and auth scheme (API-key surface)

- Base: `https://www.okx.com` (global/EEA variants `my.okx.com`, `app.okx.com`).
- Paths: `/api/v5/p2p/ad/create`, `/api/v5/p2p/ad/update`, `/api/v5/p2p/order/list` (verified), plus the enable/disable and own-ad-list paths — **names UNCONFIRMED**.
- Auth = standard OKX v5 API-key signing (quoted verbatim from the live docs page https://www.okx.com/docs-v5/en/ , section "REST Authentication"):
  - Headers: `OK-ACCESS-KEY`, `OK-ACCESS-SIGN`, `OK-ACCESS-TIMESTAMP` ("ISO 8601 UTC format with millisecond precision, e.g. `2020-12-08T09:08:57.715Z`. The server rejects requests where this differs from server time by more than 30 seconds (error 50102)"), `OK-ACCESS-PASSPHRASE`.
  - Signing: "Create a pre-hash string of timestamp + method + requestPath + body … Sign the pre-hash string with the SecretKey using the HMAC SHA256 … Encode the signature in the Base64 format." Example given: `sign=CryptoJS.enc.Base64.stringify(CryptoJS.HmacSHA256(timestamp + 'GET' + '/api/v5/account/balance?ccy=BTC', SecretKey))`. Method uppercase; `requestPath` includes the query string; body omitted when empty.
  - Content type: `application/json`.
  - RSA signing: **not observed anywhere in the retrieved documentation prefix** — do not assume it exists (UNCONFIRMED).
- Error convention (observed live): JSON body with **string** `code`; missing key gives HTTP 401 with `code:"50103"`. Success convention elsewhere in v5 is `{"code":"0","msg":"","data":[...]}` — the P2P endpoints are expected to follow it (UNCONFIRMED for P2P specifically, since I never saw an authenticated P2P response).
- Clock skew: sync with `GET /api/v5/public/time` (docs statement).

### Enable/disable, list-your-own-ads, request bodies — NOT VERIFIABLE in the time-box

The P2P API reference could not be read:
- `https://www.okx.com/docs-v5/en/` is a single ~10 MB Slate page; my reader truncates the fetch at ≈490 KB and the retrieved prefix ends inside "Order Book Trading / Trade". The byte string `p2p` does not occur anywhere in the retrievable prefix (checked with a case-insensitive regex over the fetched content), so the P2P section (if present in that page) sits beyond the truncation point. Tried: direct fetch, `?raw` paging, anchored URL `…/docs-v5/en/#rest-api-p2p-trading`, the `app.okx.com` mirror, the `zh` mirror, `llms.txt` variants (`/llms.txt`, `/docs-v5/llms.txt`, `/docs-v5/en/llms.txt` → 404), `search_data.json`/`README.md` → 404, Wayback CDX listing of `okx.com/docs-v5*` (no P2P anchor ever archived).
- Web search for the endpoints returned nothing: `"api/v5/p2p"`, `"p2p/ad/create"`, `"rest-api-p2p"` → **zero results** on DuckDuckGo; Brave/Google/Bing/Startpage/Yandex/Mojeek either returned unrelated slop or blocked automation. Consequence: the P2P API reference appears to be gated to approved merchants (consistent with the User Agreement's whitelisting clause) rather than published in the public API guide.

### Web-private alternative (session cookie surface)

- `GET https://www.okx.com/v3/c2c/tradingOrders/getMyAds` returns **HTTP 403 (application/json)** unauthenticated → the route exists and is protected by session auth. Sibling namespace `/v3/c2c/advertisement/*` is 404, so `/v3/c2c/tradingOrders/*` is the live private namespace.
- The public sibling in the same namespace (`/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin`) needs no auth at all, so the "private" part is enforced per-route, not per-namespace.
- **UNCONFIRMED**: the create/update/enable paths in this namespace (e.g. `…/createAd`, `…/updateAd`, `…/publishAd` were NOT probed and must not be assumed); the exact cookie set (OKX web uses `OK-ACCESS-TOKEN`/session cookies — unverified) and the CSRF header name; whether such endpoints are protocol-stable or protected by additional anti-bot headers when called outside a browser. To resolve: open one authenticated browser session (DevTools → Network) on the P2P "My ads" page, capture the create/update/toggle requests verbatim, and replay with the same cookie + CSRF header.

## Field mapping table

| venue field path | semantics | observed type |
| --- | --- | --- |
| `data.sell[]` / `data.buy[]` | advertisement rows; selected by request `side` | array |
| `id` | advertisement id (e.g. `"260924231439195"`) | string |
| `side` | tradeType/side enum: `"sell"` \| `"buy"` (as returned by this endpoint; lowercase) | string |
| `price` | advertised unit price in fiat per 1 crypto | string |
| `quoteMinAmountPerOrder` | min single-trade amount, in **fiat** (quote currency) | string |
| `quoteMaxAmountPerOrder` | max single-trade amount, in **fiat** (quote currency) | string |
| `availableAmount` | tradable/available crypto amount | string |
| `baseCurrency` / `quoteCurrency` | `"usdt"` / `"uah"`\|`"pln"` (lowercase) | string |
| `quoteScale` / `quoteSymbol` | fiat decimals (2) / fiat glyph (`₴`,`zł`) | number / string |
| `paymentMethods` | payment method names, e.g. `"PrivatBank (CARD)"`, `"BLIK"`, `"bank"` (mixed display names and generic codes) | array of string |
| `paymentTimeoutMinutes` | pay window in minutes (15 observed, 10 for one buy row) | number |
| `creatorType` | merchant flag: `"diamond"` \| `"certified"` \| `"common"` | string |
| `merchantId` | merchant id; `""` for non-merchant makers | string |
| `badgeInfo.badgeList[].badgeId` / `.title` | `-1000` = "Diamond Merchant", `-1001` = "Super Merchant" | number / string |
| `userType` | echoed request value; observed `"all"` and `"common"` — **not** a reliable merchant flag (see `creatorType`) | string |
| `completedOrderQuantity` | lifetime completed orders (reputation, not restricted to 30 days) | number |
| `completedRate` | completion rate as a **fraction** string with 4 dp: `"0.9483"` = 94.83% (NOT 0–100) | string |
| `posReviewPercentage` | positive-review percentage; observed `"-1"` (= unavailable) in every row | string |
| `cancelledOrderQuantity` | lifetime cancelled orders | number |
| `nickName` | public nickname (may be masked e-mail, e.g. `"ser***@gmail.com"`) | string |
| `publicUserId` | public user hash (profile key) | string |
| `boostType` | `"PAID"` \| `null` (featured/boosted ad) | string or null |
| `minSellOrders` / `minCompletedOrderQuantity` / `minCompletionRate` / `minKycLevel` | maker's **counterparty** requirements (min 30-day-ish order count / completion rate / KYC), not the maker's own stats | number / number / string / number |
| `data.total` | total matching ads (paging) | number |
| `code` / `error_code` | app-level status: int `0` / string `"0"` | int / string |

**30-day figures**: no 30-day order-count or 30-day volume field exists in this response. The closest fields are `completedOrderQuantity` (lifetime) and the maker *requirements* `minSellOrders`/`minCompletedOrderQuantity`. UNCONFIRMED whether any OKX endpoint exposes a rolling-30-day count — the profile page (`https://www.okx.com/p2p/profile/<publicUserId>` style URLs) may, but that page was not fetched.

## Open questions / uncertainty

1. **P2P API reference (biggest gap).** Exact request body of `POST /api/v5/p2p/ad/create` and `POST /api/v5/p2p/ad/update` (field names/types for price, min/max amount, available amount, payment methods, side, status), and the real paths for *list my ads* and *enable/disable* (candidates `ad/enable`,`ad/disable`,`ad/status`,`ad/my-ads`,`ad/query-ads`,`ad/list` all returned 404). Resolve by: getting merchant access (https://www.okx.com/p2p/api → merchant team), then reading the P2P section of the API guide — or by fetching `https://www.okx.com/docs-v5/en/` with a client that can read past ~500 KB (curl/`python -c urllib` in the project env) and grepping `id='rest-api-p2p` / `api/v5/p2p`.
2. **Does the public API guide document P2P at all?** UNCONFIRMED. Evidence against public documentation: zero search-engine hits for `"api/v5/p2p"`/`"p2p/ad/create"`, no P2P anchor in the Wayback CDX of `okx.com/docs-v5*`. Evidence for the API existing: live 405/401 probes above.
3. **Enable/disable semantics.** Whether toggling is a dedicated endpoint or a status field on `/api/v5/p2p/ad/update` — UNCONFIRMED.
4. **v5 P2P rate limits and error-code table.** Not retrieved; the user agreement only requires "reasonable API rate limits". UNCONFIRMED.
5. **Web-private session surface.** Exact create/update/enable paths, cookie names, and CSRF header for `https://www.okx.com/v3/c2c/tradingOrders/*`. UNCONFIRMED — only `/getMyAds` → 403 is proven.
6. **Anti-bot posture of the public endpoint.** It worked anonymously from a datacenter IP with no extra headers at the time of testing; UNCONFIRMED whether OKX applies UA/IP heuristics intermittently (other OKX hosts did: `docs.okx.com` DNS fails, `/p2p/api` 404s from EEA/US edges, `www.okx.com/en-us/p2p/api` 404 for me while search engines index it).
7. **`completedRate` semantics edge case**: whether it is strictly `completed/(completed+cancelled)` and whether it is rounded to 4 dp — value shape (`"0.9483"`) is confirmed; formula UNCONFIRMED.
8. **`posReviewPercentage`** was `"-1"` in 100% of rows sampled — UNCONFIRMED whether any OKX ad ever carries a real value (a positive-review-rate field may only exist on the merchant profile, not in the ad list).

### Probe log (for reproducibility)

- `GET /api/v5/account/balance` → 401, `{"msg":"Request header OK-ACCESS-KEY can not be empty.","code":"50103"}`
- `GET /api/v5/zzz/nonexistent` → 404
- `GET /api/v5/p2p/ad/create` → **405**; `GET /api/v5/p2p/ad/update` → **405**
- `GET /api/v5/p2p/order/list` → **401**
- 404s: `/api/v5/p2p/ad/{query-ads,query,query-list,list,detail,amend,enable,disable,update-sell-ad,update-buy-ad,status,my-ads,get-list,delete}`, `/api/v5/p2p/ads`
- `GET /v3/c2c/tradingOrders/getMyAds` → **403**; `GET /v3/c2c/advertisement/getMyAds` → 404; `GET /v3/c2c/paymentMethod/list` → 404
- `GET /v3/c2c/tradingOrders/getMarketplaceAdsPrelogin` (UAH sell, PLZ buy) → 200 JSON, samples above
