# Ground-truth live probes (architect, 2026-09-24)

Raw captures from this dev box (Python 3.14 + `urllib`, desktop Chrome UA). These are the
adapter implementations' primary evidence; `docs/research/<venue>.md` (investigator reports)
supplement them for the *private* advertisement APIs, which cannot be probed without
credentials.

## Binance — `POST https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search`

Request (unauthenticated, works with a plain desktop UA — no CSRF/UUID headers needed):

```json
{"page": 1, "rows": 8, "payTypes": [], "publisherType": "merchant",
 "asset": "USDT", "fiat": "UAH", "tradeType": "SELL"}
```

Observed ladder (HTTP 200, `total` 1406 with `publisherType: null`, 883 with `"merchant"`):

| price | advertiser.userType | monthOrderCount |
|---|---|---|
| 53.93 | user | 2 |
| 48.20 | merchant | 1257 |
| 48.00 | merchant | 1143 |
| 47.95 | merchant | 1071 |
| 47.70 | merchant | 964 |
| 47.50 | merchant | 1710 |

**Side semantics (settled empirically).** `tradeType="SELL"` returns the *ask* ads —
advertisers who SELL crypto, i.e. our competitors, priced 47.44–48.20. `tradeType="BUY"`
returns the bid ads (45.54–45.83, e.g. merchant `ExTurk`, monthOrderCount 35059). Bids below
asks is the only economically possible reading, and it corroborates OKX's unambiguous
`side=sell` ladder (46.09–46.96) and the requirement's own example (`UAH/USDT` 47.00).
The response field `adv.tradeType` is **mirrored** (it reports the *taker* side: a
`tradeType="SELL"` request returns items whose `adv.tradeType` is `"BUY"`), so the adapters
MUST NOT derive an advertisement's side from `adv.tradeType`.

Field shapes confirmed live:
`data[].adv.{advNo, tradeType, asset, fiatUnit, price(str), surplusAmount(str),
tradableQuantity(str), maxSingleTransAmount(str), minSingleTransAmount(str),
tradeMethods[].identifier/tradeMethodName}`,
`data[].advertiser.{userType("merchant"|"user"), monthOrderCount(int), monthFinishRate(float 0..1),
positiveRate(float 0..1), nickName, userNo}`.
`monthFinishRate`/`positiveRate` are fractions (`1.0`, `0.98979591`) — the 0..1 scale.
`publisherType: "merchant"` is honoured server-side and drops non-merchant noise.

## OKX — `GET https://www.okx.com/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin`

Query: `paymentMethod=all&side=sell&userType=all&sortType=price_asc&limit=100&cryptoCurrency=USDT
&fiatCurrency=UAH&currentPage=1&numberPerPage=20&t=<epoch_ms>` → HTTP 200,
`{"code":0,"data":{"sell":[...],"buy":[...],"total":100}}`.

`side=sell` → 46.09–46.96 (asks); `side=buy` → 40.46–43.00 (bids). Unambiguous: `side` names
the **advertiser's** side, so the parser requests `side=sell`.

Row fields confirmed live:
`price(str), availableAmount(str), quoteMinAmountPerOrder(str), quoteMaxAmountPerOrder(str),
nickName, id(ad id, str), merchantId(str), completedOrderQuantity(int), completedRate(str 0..1),
creatorType("diamond"|"certified"|"common"), intention(bool), paymentMethods[list[str]],
isInstitution(int), badgeInfo.badgeList[].title`.

**Merchant filter.** The `userType=merchant` *query* parameter has no effect on the response
(rows still return `userType: "all"` and the same ads). The advertiser class is
`creatorType` — non-`common` values (`diamond`, `certified`) always carry a non-empty
`merchantId`, while every `common` row has `merchantId: ""`. Filter = merchant tier in
{diamond, certified, super} OR (non-empty `merchantId` and `creatorType != "common"`).
Example merchant row: `Anticorupcioner`, `creatorType: "diamond"`, `merchantId: a5e7627aea`,
`completedOrderQuantity: 680`, `completedRate: "0.9483"`.

## Bybit — `POST https://www.bybit.com/x-api/fiat/otc/item/recommend/online`

HTTP **403 Access Denied** (Akamai edge, `errors.edgesuite.net`) for a plain client,
unauthenticated and with the desktop UA, from this host. The requirement makes this
irrelevant for pricing (ByBit copies Binance), but it means:
1. the Bybit adapter's public-search path cannot be verified live from here, and
2. the Bybit *private* P2P API may likewise be blocked without a browser session.

The adapter therefore implements the documented public-search request shape, and the
end-to-end Bybit price comes from `copy:Binance` (SPEC §7 step 5) — never from a Bybit fetch.

## Private publish endpoints — live probes, 2026-09-24 (placeholder credentials)

Built by the adapters themselves (`build_create_ad_request` / `build_update_ad_request`) and
sent to production with a placeholder key. Every venue answered with an **auth error naming
only the key** — so path, method and signing scheme are accepted; the credentials are the
single missing ingredient. A 404/HTML/WAF answer would have falsified the endpoint choice.

| venue | endpoint | live answer |
|---|---|---|
| Binance | `POST https://api.binance.com/sapi/v1/c2c/agent/ads/post` | `HTTP 400 {"code":-2008,"msg":"Invalid Api-Key ID."}` |
| Binance | `POST …/sapi/v1/c2c/agent/ads/update` | `HTTP 400 {"code":-2008,…}` |
| Binance | `POST …/sapi/v1/c2c/agent/ads/listWithPagination` | `HTTP 400 {"code":-2008,…}` |
| Binance | `POST …/sapi/v1/c2c/agent/ads/getDetailByNo` | `HTTP 400 {"code":-2008,…}` |
| OKX | `POST https://www.okx.com/api/v5/p2p/ad/create` | `HTTP 401 {"msg":"Invalid OK-ACCESS-KEY","code":"50111"}` |
| OKX | `POST https://www.okx.com/api/v5/p2p/ad/update` | `HTTP 401 {"code":"50111"}` |
| Bybit | `POST https://api.bybit.com/v5/p2p/item/create` | `HTTP 200 {"ret_code":10003,"ret_msg":"API key is invalid."}` |
| Bybit | `POST https://api.bybit.com/v5/p2p/item/update` | `HTTP 200 {"ret_code":10003,…}` |

Signing headers observed in the built requests: Binance `X-MBX-APIKEY` (HMAC-SHA256 over the
sorted query), OKX `OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE`, Bybit
`X-BAPI-API-KEY/SIGN/TIMESTAMP/RECV-WINDOW`.

Two corrections to the notes above:

1. **OKX P2P ad management does exist as a signed API** (`/api/v5/p2p/ad/create`,
   `/api/v5/p2p/ad/update`). The earlier "merchant-only, no public ad API" reading was about
   the *public* marketplace endpoints; the private P2P ad path answers signed requests.
2. **The Bybit private P2P API is *not* behind the `www` WAF**: `www.bybit.com/x-api/...` is
   Akamai-blocked for a plain client, but `api.bybit.com/v5/p2p/item/*` is reachable and
   validates signed requests normally.

Also confirmed live: Binance's `build_create_ad_request` performs one authenticated lookup
(`_resolve_trade_methods`, mapping blueprint payment-method names to venue IDs) before it can
build the request, which is why a build with placeholder credentials raises `ApiError` — by
design, and the reason an operator sees a credential error before any ad is attempted.
