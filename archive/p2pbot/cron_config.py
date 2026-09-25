"""Cron settings for the PLN buy-ad edit queue. Edit the two values below.

SCHEDULE_TIME  minutes between two runs of the edit queue: the ``pln-edits`` job of
               ``python run.py bot`` reprices the live PLN buy ads this often.
PAIRS          the pairs the queue reprices, as crypto tickers (``"USDT"`` means
               ``PLN/USDT``) or full pair symbols (``"PLN/USDT"``). Every pair must be a PLN
               pair of the active scenario (``scenarios/pln.json``).

Both values are checked when the bot starts (and by ``python run.py verify-config``): a
bad value stops it with a message naming the setting, instead of running on a guess.
"""

SCHEDULE_TIME = 25

PAIRS = ["USDT", "USDC"]


# --------------------------------------------------------------------------------------
# validation - nothing to configure below this line
# --------------------------------------------------------------------------------------
from dataclasses import dataclass  # noqa: E402 - the settings stay at the top of the file
from typing import Any  # noqa: E402

from .errors import ConfigError  # noqa: E402
from .models import Pair  # noqa: E402

#: The only fiat the edit queue handles.
CRON_FIAT = "PLN"


@dataclass(frozen=True)
class CronConfig:
    """Validated cron settings: the job interval and the pairs it edits."""

    interval_minutes: int
    pairs: tuple[Pair, ...]

    def describe(self) -> str:
        return (
            f"every {self.interval_minutes} min, pairs "
            + ", ".join(pair.symbol for pair in self.pairs)
        )


def load_cron_config(schedule_time: Any = None, pairs: Any = None) -> CronConfig:
    """Validate the settings (the module values by default) into a :class:`CronConfig`.

    Raises :class:`ConfigError` naming the offending setting.
    """
    schedule_time = SCHEDULE_TIME if schedule_time is None else schedule_time
    pairs = PAIRS if pairs is None else pairs
    if isinstance(schedule_time, bool) or not isinstance(schedule_time, int) or schedule_time <= 0:
        raise ConfigError(
            f"cron_config.SCHEDULE_TIME must be a positive whole number of minutes, "
            f"got {schedule_time!r}"
        )
    if isinstance(pairs, str) or not isinstance(pairs, (list, tuple)) or not pairs:
        raise ConfigError(
            f'cron_config.PAIRS must be a non-empty list like ["USDT", "USDC"], got {pairs!r}'
        )
    resolved: list[Pair] = []
    for item in pairs:
        pair = _parse_pair(item)
        if pair in resolved:
            raise ConfigError(f"cron_config.PAIRS lists {pair.symbol} twice")
        resolved.append(pair)
    return CronConfig(interval_minutes=schedule_time, pairs=tuple(resolved))


def _parse_pair(item: Any) -> Pair:
    """``"USDT"`` -> ``PLN/USDT``; ``"PLN/USDT"`` stays; anything else is refused."""
    if not isinstance(item, str) or not item.strip():
        raise ConfigError(f"cron_config.PAIRS entries must be tickers like \"USDT\", got {item!r}")
    text = item.strip()
    symbol = text if "/" in text else f"{CRON_FIAT}/{text}"
    try:
        pair = Pair.parse(symbol)
    except ConfigError as exc:
        raise ConfigError(f"cron_config.PAIRS entry {item!r} is not a valid ticker: {exc}") from exc
    if pair.fiat != CRON_FIAT:
        raise ConfigError(
            f"cron_config.PAIRS entry {item!r} is not a {CRON_FIAT} pair; the edit queue "
            f"only handles {CRON_FIAT} pairs"
        )
    return pair
