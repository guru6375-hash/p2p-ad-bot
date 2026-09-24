"""P2P advertisement manager: a Telegram-driven price manager for Binance/OKX/ByBit P2P.

This module deliberately imports no submodules, so importing any single module (for
example ``p2pbot.config``) can never trigger an import cycle. Import what you need
directly::

    from p2pbot.config import load_settings

The package version is re-exported from :mod:`p2pbot.constants` (the single source of
truth for :data:`VERSION`) so callers can do ``import p2pbot; p2pbot.__version__``.
"""

from __future__ import annotations

from .constants import VERSION

__all__ = ["__version__"]

__version__ = VERSION
