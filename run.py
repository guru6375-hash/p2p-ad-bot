"""Entry point: ``python run.py <command>`` (SPEC section 12).

Thin on purpose - everything lives in :mod:`p2pbot.cli` so the CLI stays testable through
``p2pbot.cli.main(argv, env=...)``.
"""

from __future__ import annotations

from p2pbot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
