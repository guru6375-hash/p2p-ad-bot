"""Scenario blueprints: the per-scenario price plan (SPEC section 4).

A blueprint is the *custom trading scenario* — which pairs to advertise, with which
accounts, and where each platform's price comes from. :func:`parse_blueprint` validates a
decoded JSON mapping and resolves every per-platform decision (source expression, filters,
platform accounts) up front, so the engine and the market parser never have to guess.

Hard rules encoded here:

* ``fixed_spread`` scenarios may not carry spread values — the spread lives in
  :mod:`p2pbot.constants` (``UAH_SPREAD``).
* a non-anchor pair must resolve ``linked_to`` (auto-linked to the sibling anchor for UAH).
* ``copy:<Platform>`` must point at a real, non-copy plan of the same pair.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .constants import DEFAULT_PARSER_INTERVAL_MINUTES, FILTERS_BY_PLATFORM, PLATFORMS
from .errors import BlueprintError, ConfigError
from .models import AccountRef, Filters, Pair, parse_decimal

__all__ = [
    "Blueprint",
    "Defaults",
    "PairPlan",
    "ParserConfig",
    "load_blueprint",
    "load_blueprint_by_name",
    "parse_blueprint",
]

BLUEPRINT_VERSION = 1
SUPPORTED_FIATS: tuple[str, ...] = ("UAH", "PLN")

STRATEGY_FIXED_SPREAD = "fixed_spread"
STRATEGY_MARKET_MIDDLE = "market_middle"
STRATEGIES: tuple[str, ...] = (STRATEGY_FIXED_SPREAD, STRATEGY_MARKET_MIDDLE)

SOURCE_BASE_RATE = "base_rate"
SOURCE_BASE_RATE_MINUS_SPREAD = "base_rate_minus_spread"
SOURCE_MARKET_MIDDLE = "market_middle"
COPY_PREFIX = "copy:"
SOURCE_EXPRESSIONS: tuple[str, ...] = (
    SOURCE_BASE_RATE,
    SOURCE_BASE_RATE_MINUS_SPREAD,
    SOURCE_MARKET_MIDDLE,
    f"{COPY_PREFIX}<Platform>",
)

#: ``market_middle`` scenarios derive ByBit from Binance (SPEC 4.1).
MARKET_MIDDLE_COPY_SOURCE = f"{COPY_PREFIX}{PLATFORMS[0].capitalize()}"

#: Spread keys that would hardcode what ``constants.UAH_SPREAD`` fixes. HARDCODED.
HARDCODED_SPREAD_KEYS: tuple[str, ...] = ("spread", "min_spread", "spread_uah", "diff")
HARDCODED_SPREAD_MESSAGE = "spread for fixed_spread scenarios is hardcoded per platform"

#: Fallbacks used when a blueprint (or a pair) omits the amounts entirely.
DEFAULT_MIN_AMOUNT = Decimal("1")
DEFAULT_MAX_AMOUNT = Decimal("1000000")
DEFAULT_PRICE_OFFSET = Decimal("0")

#: Auto-linking of a non-anchor pair to the sibling anchor is defined for UAH (SPEC 4.1).
AUTO_LINK_FIAT = "UAH"

_TOP_LEVEL_KEYS = frozenset({"version", "name", "fiat", "strategy", "parser", "defaults", "pairs"})
_PAIR_KEYS = frozenset(
    {
        "pair",
        "anchor",
        "linked_to",
        "accounts",
        "min_amount",
        "max_amount",
        "payment_methods",
        "price_offset",
        "enabled",
        "platforms",
        "filters",
    }
)
_PLATFORM_KEYS = frozenset({"source", "accounts"})
_PARSER_KEYS = frozenset({"enabled", "interval_minutes", "cron"})
_DEFAULTS_KEYS = frozenset({"min_amount", "max_amount", "payment_methods", "price_offset", "filters"})


@dataclass(frozen=True)
class ParserConfig:
    """When and how often the competitor parser runs for this scenario."""

    enabled: bool = False
    interval_minutes: int = DEFAULT_PARSER_INTERVAL_MINUTES
    cron: str | None = None


@dataclass(frozen=True)
class PairPlan:
    """One pair of a blueprint with every per-platform decision already resolved.

    ``sources`` only covers the platforms that are actually part of the plan (implied by
    the pair's accounts, an explicit ``platforms`` block or a per-platform account
    override) and is ordered by :data:`p2pbot.constants.PLATFORMS`.
    """

    pair: Pair
    anchor: bool
    linked_to: Pair | None
    accounts: tuple[str, ...]
    min_amount: Decimal
    max_amount: Decimal
    payment_methods: tuple[str, ...]
    price_offset: Decimal
    enabled: bool
    sources: Mapping[str, str]
    filters: Mapping[str, Filters] = field(default_factory=dict)
    platform_accounts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def accounts_for(self, platform: str) -> tuple[str, ...]:
        """The account ids to advertise with on ``platform``, ordered by account index."""
        key = platform.strip().lower()
        override = self.platform_accounts.get(key)
        if override is not None:
            accounts = list(override)
        else:
            accounts = [account for account in self.accounts if _account_platform(account) == key]
        return tuple(sorted(accounts, key=_account_index))


@dataclass(frozen=True)
class Defaults:
    """Blueprint-wide fallbacks; ``filters`` holds the effective per-platform filters."""

    min_amount: Decimal = DEFAULT_MIN_AMOUNT
    max_amount: Decimal = DEFAULT_MAX_AMOUNT
    payment_methods: tuple[str, ...] = ()
    price_offset: Decimal = DEFAULT_PRICE_OFFSET
    filters: Mapping[str, Filters] = field(default_factory=dict)


@dataclass(frozen=True)
class Blueprint:
    """A validated scenario, ready to drive the engine."""

    name: str
    fiat: str
    strategy: str
    version: int
    parser: ParserConfig
    pairs: tuple[PairPlan, ...]
    defaults: Defaults = field(default_factory=Defaults)

    def pair(self, symbol: str | Pair) -> PairPlan:
        """The plan of ``symbol``, e.g. ``"UAH/USDT"``."""
        try:
            target = Pair.parse(symbol)
        except ConfigError as exc:
            raise BlueprintError(f"blueprint {self.name!r}: {exc}") from exc
        for plan in self.pairs:
            if plan.pair == target:
                return plan
        known = ", ".join(plan.pair.symbol for plan in self.pairs) or "none"
        raise BlueprintError(
            f"pair {target.symbol} is not part of blueprint {self.name!r}; known pairs: {known}"
        )

    def enabled_pairs(self) -> tuple[PairPlan, ...]:
        """Every pair plan whose ``enabled`` flag is set."""
        return tuple(plan for plan in self.pairs if plan.enabled)


@dataclass
class _PairEntry:
    """Mutable half-finished pair plan used while the whole blueprint is resolved."""

    pair: Pair
    anchor: bool
    linked_symbol: str | None
    accounts: tuple[str, ...]
    platform_accounts: dict[str, tuple[str, ...]]
    platform_sources: dict[str, str]
    filters: dict[str, Filters]
    min_amount: Decimal
    max_amount: Decimal
    payment_methods: tuple[str, ...]
    price_offset: Decimal
    enabled: bool


def parse_blueprint(data: Mapping[str, Any]) -> Blueprint:
    """Validate a decoded blueprint mapping and return a :class:`Blueprint`."""
    if not isinstance(data, Mapping):
        raise BlueprintError(f"blueprint must be a JSON object, got {type(data).__name__}")
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "blueprint")

    version = _parse_version(data.get("version"))
    name = _parse_name(data.get("name"))
    fiat = _parse_fiat(data.get("fiat"))
    strategy = _parse_strategy(data.get("strategy"))
    parser = _parse_parser(data.get("parser"))
    defaults = _parse_defaults(data.get("defaults"))
    entries = _parse_pairs(data.get("pairs"), fiat, defaults)
    plans = _resolve_pairs(entries, strategy)
    return Blueprint(
        name=name,
        fiat=fiat,
        strategy=strategy,
        version=version,
        parser=parser,
        pairs=plans,
        defaults=defaults,
    )


def load_blueprint(path: str | Path) -> Blueprint:
    """Read and validate the blueprint at ``path``."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BlueprintError(f"blueprint file not found: {file_path}") from exc
    except OSError as exc:
        raise BlueprintError(f"cannot read blueprint {file_path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlueprintError(f"blueprint {file_path} is not valid JSON: {exc}") from exc
    return parse_blueprint(data)


def load_blueprint_by_name(name: str, scenarios_dir: str | Path = "scenarios") -> Blueprint:
    """Load ``uah``/``uah.json`` (or a path) from ``scenarios_dir``."""
    if not isinstance(name, str) or not name.strip():
        raise BlueprintError("scenario name must be a non-empty string")
    filename = Path(name.strip()).name
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    directory = Path(scenarios_dir)
    target = directory / filename
    if not target.is_file():
        available = ", ".join(path.name for path in sorted(directory.glob("*.json"))) or "none"
        raise BlueprintError(
            f"unknown scenario {name!r}: {target} does not exist (available: {available})"
        )
    return load_blueprint(target)


# -- schema helpers ---------------------------------------------------------------


def _reject_unknown_keys(block: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in block if key not in allowed)
    if unknown:
        expected = ", ".join(sorted(allowed))
        raise BlueprintError(
            f"{where}: unknown key(s) {', '.join(unknown)}; expected one of: {expected}"
        )


def _check_hardcoded_spread(block: Mapping[str, Any], where: str) -> None:
    for key in HARDCODED_SPREAD_KEYS:
        if key in block:
            raise BlueprintError(f"{where}: {HARDCODED_SPREAD_MESSAGE}; remove the {key!r} key")


def _parse_version(raw: Any) -> int:
    if isinstance(raw, bool) or raw is None:
        raise BlueprintError(f"blueprint 'version' is required and must be {BLUEPRINT_VERSION}")
    if isinstance(raw, int):
        version = raw
    elif isinstance(raw, str) and raw.strip().isdecimal():
        version = int(raw.strip())
    else:
        raise BlueprintError(f"blueprint 'version' must be {BLUEPRINT_VERSION}, got {raw!r}")
    if version != BLUEPRINT_VERSION:
        raise BlueprintError(
            f"unsupported blueprint version {version}; this build supports version {BLUEPRINT_VERSION}"
        )
    return version


def _parse_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BlueprintError("blueprint 'name' must be a non-empty string")
    return raw.strip()


def _parse_fiat(raw: Any) -> str:
    if not isinstance(raw, str) or raw.strip().upper() not in SUPPORTED_FIATS:
        raise BlueprintError(
            f"blueprint 'fiat' must be one of {', '.join(SUPPORTED_FIATS)}, got {raw!r}"
        )
    return raw.strip().upper()


def _parse_strategy(raw: Any) -> str:
    if not isinstance(raw, str) or raw.strip().lower() not in STRATEGIES:
        raise BlueprintError(
            f"blueprint 'strategy' must be one of {', '.join(STRATEGIES)}, got {raw!r}"
        )
    return raw.strip().lower()


def _parse_bool(raw: Any, where: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    raise BlueprintError(f"{where} must be true or false, got {raw!r}")


def _parse_amount(raw: Any, where: str) -> Decimal:
    try:
        amount = parse_decimal(raw, where)
    except ConfigError as exc:
        raise BlueprintError(str(exc)) from exc
    if amount <= 0:
        raise BlueprintError(f"{where} must be greater than 0, got {amount}")
    return amount


def _parse_offset(raw: Any, where: str) -> Decimal:
    try:
        return parse_decimal(raw, where)
    except ConfigError as exc:
        raise BlueprintError(str(exc)) from exc


def _parse_payment_methods(raw: Any, default: tuple[str, ...], where: str) -> tuple[str, ...]:
    if raw is None:
        return tuple(default)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise BlueprintError(f"{where} must be a list of payment-method names")
    methods: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise BlueprintError(f"{where}: payment method must be a non-empty string, got {item!r}")
        method = item.strip()
        if method not in methods:
            methods.append(method)
    return tuple(methods)


def _parse_pair_symbol(raw: Any, where: str) -> Pair:
    if not isinstance(raw, str):
        raise BlueprintError(f"{where}: 'pair' must be a string like UAH/USDT")
    try:
        return Pair.parse(raw)
    except ConfigError as exc:
        raise BlueprintError(f"{where}: {exc}") from exc


def _parse_accounts(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise BlueprintError(f"{where} must be a list of account ids like Binance#1")
    accounts: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise BlueprintError(f"{where}: account id must be a string, got {type(item).__name__}")
        try:
            ref = AccountRef.parse(item)
        except ConfigError as exc:
            raise BlueprintError(f"{where}: {exc}") from exc
        if ref.platform not in PLATFORMS:
            raise BlueprintError(
                f"{where}: account {ref.id} uses unknown platform {ref.platform!r}; "
                f"known platforms: {', '.join(PLATFORMS)}"
            )
        if ref.id not in accounts:
            accounts.append(ref.id)
    return tuple(accounts)


def _parse_source_expression(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BlueprintError(f"{where} must be a source expression, got {raw!r}")
    text = raw.strip()
    lowered = text.lower()
    if lowered in (SOURCE_BASE_RATE, SOURCE_BASE_RATE_MINUS_SPREAD, SOURCE_MARKET_MIDDLE):
        return lowered
    if lowered.startswith(COPY_PREFIX):
        platform = lowered[len(COPY_PREFIX) :].strip()
        if platform not in PLATFORMS:
            raise BlueprintError(
                f"{where}: unknown platform {platform!r} in {text!r}; "
                f"known platforms: {', '.join(PLATFORMS)}"
            )
        return f"{COPY_PREFIX}{platform.capitalize()}"
    raise BlueprintError(
        f"{where}: unknown source expression {text!r}; allowed: {', '.join(SOURCE_EXPRESSIONS)}"
    )


def _base_filters(platform: str) -> Filters:
    return FILTERS_BY_PLATFORM.get(platform, Filters())


def _merge_filters(raw: Any, base_by_platform: Mapping[str, Filters], where: str) -> dict[str, Filters]:
    resolved = dict(base_by_platform)
    if raw is None:
        return resolved
    if not isinstance(raw, Mapping):
        raise BlueprintError(f"{where} must be a JSON object mapping platform -> filter object")
    _check_hardcoded_spread(raw, where)
    for platform_key, block in raw.items():
        platform = str(platform_key).strip().lower()
        if platform not in PLATFORMS:
            raise BlueprintError(
                f"{where}: unknown platform {platform_key!r}; known platforms: {', '.join(PLATFORMS)}"
            )
        if isinstance(block, Mapping):
            _check_hardcoded_spread(block, f"{where}.{platform}")
        try:
            resolved[platform] = Filters.from_dict(block, base=resolved[platform])
        except ConfigError as exc:
            raise BlueprintError(f"{where}.{platform}: {exc}") from exc
    return resolved


def _parse_platforms(raw: Any, where: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    sources: dict[str, str] = {}
    accounts: dict[str, tuple[str, ...]] = {}
    if raw is None:
        return sources, accounts
    if not isinstance(raw, Mapping):
        raise BlueprintError(f"{where} must be a JSON object mapping platform -> plan overrides")
    for platform_key, block in raw.items():
        platform = str(platform_key).strip().lower()
        if platform not in PLATFORMS:
            raise BlueprintError(
                f"{where}: unknown platform {platform_key!r}; known platforms: {', '.join(PLATFORMS)}"
            )
        if not isinstance(block, Mapping):
            raise BlueprintError(f"{where}.{platform} must be a JSON object")
        _check_hardcoded_spread(block, f"{where}.{platform}")
        _reject_unknown_keys(block, _PLATFORM_KEYS, f"{where}.{platform}")
        if "source" in block:
            sources[platform] = _parse_source_expression(block["source"], f"{where}.{platform}.source")
        if "accounts" in block:
            accounts[platform] = _parse_accounts(block["accounts"], f"{where}.{platform}.accounts")
    return sources, accounts


def _parse_parser(raw: Any) -> ParserConfig:
    if raw is None:
        return ParserConfig()
    if not isinstance(raw, Mapping):
        raise BlueprintError("'parser' must be a JSON object")
    _reject_unknown_keys(raw, _PARSER_KEYS, "parser")

    enabled = _parse_bool(raw.get("enabled", False), "parser.enabled")
    interval_raw = raw.get("interval_minutes", DEFAULT_PARSER_INTERVAL_MINUTES)
    if isinstance(interval_raw, bool) or not isinstance(interval_raw, int):
        if isinstance(interval_raw, str) and interval_raw.strip().isdecimal():
            interval = int(interval_raw.strip())
        else:
            raise BlueprintError(
                f"parser.interval_minutes must be a positive integer, got {interval_raw!r}"
            )
    else:
        interval = interval_raw
    if interval <= 0:
        raise BlueprintError(f"parser.interval_minutes must be greater than 0, got {interval}")

    cron = raw.get("cron")
    if cron is not None:
        if not isinstance(cron, str) or not cron.strip():
            raise BlueprintError("parser.cron must be a cron string like '*/25 * * * *'")
        cron = cron.strip()
    return ParserConfig(enabled=enabled, interval_minutes=interval, cron=cron)


def _parse_defaults(raw: Any) -> Defaults:
    base_filters = {platform: _base_filters(platform) for platform in PLATFORMS}
    if raw is None:
        return Defaults(filters=base_filters)
    if not isinstance(raw, Mapping):
        raise BlueprintError("'defaults' must be a JSON object")
    _reject_unknown_keys(raw, _DEFAULTS_KEYS, "defaults")

    min_amount = _parse_amount(raw.get("min_amount", DEFAULT_MIN_AMOUNT), "defaults.min_amount")
    max_amount = _parse_amount(raw.get("max_amount", DEFAULT_MAX_AMOUNT), "defaults.max_amount")
    if max_amount < min_amount:
        raise BlueprintError(
            f"defaults.max_amount ({max_amount}) must be greater than or equal to "
            f"defaults.min_amount ({min_amount})"
        )
    methods = _parse_payment_methods(raw.get("payment_methods"), (), "defaults.payment_methods")
    offset = _parse_offset(raw.get("price_offset", DEFAULT_PRICE_OFFSET), "defaults.price_offset")
    filters = _merge_filters(raw.get("filters"), base_filters, "defaults.filters")
    return Defaults(
        min_amount=min_amount,
        max_amount=max_amount,
        payment_methods=methods,
        price_offset=offset,
        filters=filters,
    )


def _parse_pairs(raw: Any, fiat: str, defaults: Defaults) -> list[_PairEntry]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise BlueprintError("blueprint 'pairs' must be a non-empty list of pair objects")

    entries: list[_PairEntry] = []
    seen: dict[str, int] = {}
    for position, item in enumerate(raw):
        where = f"pairs[{position}]"
        if not isinstance(item, Mapping):
            raise BlueprintError(f"{where} must be a JSON object")
        _check_hardcoded_spread(item, where)
        _reject_unknown_keys(item, _PAIR_KEYS, where)

        pair = _parse_pair_symbol(item.get("pair"), where)
        if pair.fiat != fiat:
            raise BlueprintError(
                f"{where}: pair {pair.symbol} uses fiat {pair.fiat} but the blueprint fiat is {fiat}"
            )
        if pair.symbol in seen:
            raise BlueprintError(f"duplicate pair {pair.symbol} ({where} and pairs[{seen[pair.symbol]}])")
        seen[pair.symbol] = position

        anchor = _parse_bool(item.get("anchor", False), f"{where}.anchor")
        linked_symbol = item.get("linked_to")
        if linked_symbol is not None:
            if not isinstance(linked_symbol, str) or not linked_symbol.strip():
                raise BlueprintError(f"{where}.linked_to must be a pair string like UAH/USDT")
            if anchor:
                raise BlueprintError(
                    f"{where}: anchor pair {pair.symbol} must not declare 'linked_to'"
                )
            linked_symbol = linked_symbol.strip()

        accounts = _parse_accounts(item.get("accounts"), f"{where}.accounts")
        platform_sources, platform_accounts = _parse_platforms(
            item.get("platforms"), f"{where}.platforms"
        )
        min_amount = _parse_amount(item.get("min_amount", defaults.min_amount), f"{where}.min_amount")
        max_amount = _parse_amount(item.get("max_amount", defaults.max_amount), f"{where}.max_amount")
        if max_amount < min_amount:
            raise BlueprintError(
                f"{where}.max_amount ({max_amount}) must be greater than or equal to "
                f"min_amount ({min_amount})"
            )
        methods = _parse_payment_methods(
            item.get("payment_methods"), defaults.payment_methods, f"{where}.payment_methods"
        )
        offset = _parse_offset(item.get("price_offset", defaults.price_offset), f"{where}.price_offset")
        enabled = _parse_bool(item.get("enabled", True), f"{where}.enabled")
        filters = _merge_filters(item.get("filters"), defaults.filters, f"{where}.filters")

        entries.append(
            _PairEntry(
                pair=pair,
                anchor=anchor,
                linked_symbol=linked_symbol,
                accounts=accounts,
                platform_accounts=platform_accounts,
                platform_sources=platform_sources,
                filters=filters,
                min_amount=min_amount,
                max_amount=max_amount,
                payment_methods=methods,
                price_offset=offset,
                enabled=enabled,
            )
        )
    return entries


def _resolve_pairs(entries: Sequence[_PairEntry], strategy: str) -> tuple[PairPlan, ...]:
    anchors = [entry for entry in entries if entry.anchor]
    anchor_symbols = {entry.pair.symbol for entry in anchors}
    declared_symbols = {entry.pair.symbol for entry in entries}
    ordered = anchors + [entry for entry in entries if not entry.anchor]

    plans: list[PairPlan] = []
    for entry in ordered:
        linked_to = _linked_pair(entry, anchors, anchor_symbols, declared_symbols)
        platforms = _present_platforms(entry)
        sources = _resolve_sources(entry, platforms, linked_to, strategy)
        plans.append(
            PairPlan(
                pair=entry.pair,
                anchor=entry.anchor,
                linked_to=linked_to,
                accounts=entry.accounts,
                min_amount=entry.min_amount,
                max_amount=entry.max_amount,
                payment_methods=entry.payment_methods,
                price_offset=entry.price_offset,
                enabled=entry.enabled,
                sources=sources,
                filters=entry.filters,
                platform_accounts=entry.platform_accounts,
            )
        )
    return tuple(plans)


def _linked_pair(
    entry: _PairEntry,
    anchors: Sequence[_PairEntry],
    anchor_symbols: set[str],
    declared_symbols: set[str],
) -> Pair | None:
    if entry.anchor:
        return None
    if entry.linked_symbol is None:
        return _auto_linked_pair(entry.pair, anchors)

    target = _parse_pair_symbol(entry.linked_symbol, f"pair {entry.pair.symbol}.linked_to")
    if target == entry.pair:
        raise BlueprintError(f"pair {entry.pair.symbol} cannot link to itself")
    if target.fiat != entry.pair.fiat:
        raise BlueprintError(
            f"pair {entry.pair.symbol} links to {target.symbol}, which trades a different "
            f"fiat ({target.fiat})"
        )
    if target.symbol not in anchor_symbols:
        if target.symbol in declared_symbols:
            raise BlueprintError(
                f"pair {entry.pair.symbol} links to {target.symbol}, which is not marked as an anchor"
            )
        raise BlueprintError(
            f"pair {entry.pair.symbol} links to {target.symbol}, which is not declared in this blueprint"
        )
    return target


def _auto_linked_pair(pair: Pair, anchors: Sequence[_PairEntry]) -> Pair:
    candidates = [entry.pair for entry in anchors if entry.pair.fiat == pair.fiat]
    if pair.fiat == AUTO_LINK_FIAT and len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        symbols = ", ".join(candidate.symbol for candidate in candidates)
        raise BlueprintError(
            f"pair {pair.symbol} must declare 'linked_to': several anchor pairs trade "
            f"{pair.fiat} ({symbols})"
        )
    raise BlueprintError(
        f"pair {pair.symbol} is not an anchor and must declare 'linked_to' "
        f"(no {pair.fiat} anchor pair is available to link it to)"
    )


def _present_platforms(entry: _PairEntry) -> tuple[str, ...]:
    present = {_account_platform(account) for account in entry.accounts}
    present.update(entry.platform_sources)
    present.update(entry.platform_accounts)
    return tuple(platform for platform in PLATFORMS if platform in present)


def _resolve_sources(
    entry: _PairEntry,
    platforms: Sequence[str],
    linked_to: Pair | None,
    strategy: str,
) -> dict[str, str]:
    if strategy == STRATEGY_MARKET_MIDDLE:
        resolved = {
            platform: (
                MARKET_MIDDLE_COPY_SOURCE if platform == "bybit" else SOURCE_MARKET_MIDDLE
            )
            for platform in platforms
        }
    else:
        anchor_source = SOURCE_BASE_RATE if entry.anchor else SOURCE_BASE_RATE_MINUS_SPREAD
        resolved = {platform: anchor_source for platform in platforms}
    resolved.update(entry.platform_sources)

    for platform, source in resolved.items():
        if source == SOURCE_BASE_RATE_MINUS_SPREAD and linked_to is None:
            raise BlueprintError(
                f"pair {entry.pair.symbol}: source {SOURCE_BASE_RATE_MINUS_SPREAD!r} for "
                f"{platform} requires 'linked_to'"
            )

    for platform, source in resolved.items():
        if not source.startswith(COPY_PREFIX):
            continue
        copied = source[len(COPY_PREFIX) :].lower()
        if copied == platform:
            raise BlueprintError(f"pair {entry.pair.symbol}: platform {platform} cannot copy from itself")
        other = resolved.get(copied)
        if other is None:
            raise BlueprintError(
                f"pair {entry.pair.symbol}: source {source!r} for {platform} requires a "
                f"{copied} plan for the same pair"
            )
        if other.startswith(COPY_PREFIX):
            raise BlueprintError(
                f"pair {entry.pair.symbol}: copy chains are not allowed "
                f"({platform} copies {source} and {copied} copies {other})"
            )
    return resolved


def _account_platform(account_id: str) -> str:
    return account_id.split("#", 1)[0].strip().lower()


def _account_index(account_id: str) -> int:
    _, _, index = account_id.partition("#")
    return int(index) if index.isdecimal() else 0
