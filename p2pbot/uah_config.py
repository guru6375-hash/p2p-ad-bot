"""UAH ``/setrate`` settings. Edit STEP below.

``/setrate`` -> **UAH** -> ``RATE`` in Telegram reprices, per account, the online **buy** ads
as a ladder that goes one STEP lower per ad (ads ranked by their current price, highest
first). ``UAH/USDT`` starts at ``RATE`` and ``UAH/USDC`` one STEP lower:

    /setrate 45.00, Binance (STEP 0.25), 3 USDT + 3 USDC ads:
        UAH/USDT  45.00  44.75  44.50
        UAH/USDC  44.75  44.50  44.25
    same on ByBit (STEP 0.01):
        UAH/USDT  45.00  44.99  44.98
        UAH/USDC  44.99  44.98  44.97

STEP takes one value per exchange, as a string or a number (``"0.25"`` or ``0.25``), zero or
more. An exchange left out is skipped by ``/setrate`` UAH (OKX is off for now). STEP is
checked when the bot starts (and by ``python run.py verify-config``): a bad value stops it
with a message naming the setting.
"""

STEP = {
    "binance": "0.25",
    # "okx": "0.01",  # OKX is skipped for now
    "bybit": "0.01",
}


# --------------------------------------------------------------------------------------
# validation - nothing to configure below this line
# --------------------------------------------------------------------------------------
from decimal import Decimal  # noqa: E402 - the settings stay at the top of the file
from typing import Any  # noqa: E402

from .constants import PLATFORMS  # noqa: E402
from .errors import ConfigError  # noqa: E402
from .models import parse_decimal  # noqa: E402


def load_uah_steps(step: Any = None) -> dict[str, Decimal]:
    """Validate STEP (the module value by default) into ``{exchange: Decimal}``.

    Raises :class:`ConfigError` naming the offending setting.
    """
    step = STEP if step is None else step
    if not isinstance(step, dict):
        raise ConfigError(f'uah_config.STEP must be a dict like {{"binance": "0.25"}}, got {step!r}')
    steps: dict[str, Decimal] = {}
    for key, raw in step.items():
        exchange = str(key).strip().lower()
        if exchange not in PLATFORMS:
            raise ConfigError(
                f"uah_config.STEP has an unknown exchange {key!r}; known: {', '.join(PLATFORMS)}"
            )
        value = str(raw) if isinstance(raw, float) else raw
        try:
            amount = parse_decimal(value, f"uah_config.STEP[{exchange!r}]")
        except ConfigError as exc:
            raise ConfigError(str(exc)) from exc
        if amount < 0:
            raise ConfigError(f"uah_config.STEP[{exchange!r}] must not be negative, got {amount}")
        steps[exchange] = amount
    if not steps:
        raise ConfigError("uah_config.STEP needs a value for at least one exchange")
    return steps


def describe_steps(steps: dict[str, Decimal]) -> str:
    return ", ".join(f"{exchange} {steps[exchange]}" for exchange in PLATFORMS if exchange in steps)
