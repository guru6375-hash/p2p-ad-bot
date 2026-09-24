"""Live smoke of the public P2P endpoints through the real adapters.

Read-only: uses no credentials and never calls a private endpoint. Run it from the project
root:

    python scripts/smoke_live.py            # all pairs
    python scripts/smoke_live.py UAH/USDT   # one pair

Exit code 0 when at least one venue answered with usable data, 1 otherwise. Bybit is
expected to fail from most networks (HTTP 403 Akamai); its price never comes from a Bybit
fetch in this project (the PLN scenario copies Binance), so a Bybit failure is reported but
does not fail the smoke unless it is the only venue configured.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p2pbot.constants import FILTERS_BY_PLATFORM  # noqa: E402
from p2pbot.errors import ExchangeError  # noqa: E402
from p2pbot.exchanges import build_adapters  # noqa: E402
from p2pbot.models import Pair  # noqa: E402

DEFAULT_PAIRS = ("UAH/USDT", "UAH/USDC", "PLN/USDT", "PLN/USDC")


def main(argv: list[str]) -> int:
    symbols = argv[1:] or list(DEFAULT_PAIRS)
    adapters = build_adapters()
    usable = 0
    for symbol in symbols:
        pair = Pair.parse(symbol)
        print(f"\n=== {pair.symbol} ===")
        for platform, adapter in adapters.items():
            filters = FILTERS_BY_PLATFORM.get(platform)
            try:
                snapshot = adapter.search_ads(pair, filters=filters)
            except ExchangeError as exc:
                print(f"  {platform:<8} FAILED  {type(exc).__name__}: {str(exc)[:120]}")
                continue
            prices = sorted(ad.price for ad in snapshot.filtered)
            print(
                f"  {platform:<8} fetched={len(snapshot.ads):<3} kept={len(snapshot.filtered):<3} "
                f"middle={snapshot.middle} range={prices[0] if prices else '-'}..{prices[-1] if prices else '-'}"
            )
            for ad in snapshot.filtered[:3]:
                print(
                    f"      {ad.price} {ad.advertiser or '?':<24} orders={ad.month_order_count} "
                    f"pos={ad.positive_rate} finish={ad.month_finish_rate} type={ad.user_type}"
                )
            usable += 1
    print(f"\nvenues answering: {usable}/{len(symbols) * len(adapters)}")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
