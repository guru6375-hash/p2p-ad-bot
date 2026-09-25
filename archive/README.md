# Archive

Code taken out of the running bot. None of it is imported by `p2pbot/` or collected by
pytest (`pytest.ini` skips this folder). The bot now does only two things: `/getads` and
`/setrate` (UAH ladder or PLN flat rate, picked with a button).

| What | Where it was | Why it is here |
|---|---|---|
| `p2pbot/cron.py`, `p2pbot/scheduler.py` | `p2pbot/` | cron expressions and the job scheduler (parser job, `pln-edits` job) |
| `p2pbot/cron_config.py` | `p2pbot/` | `SCHEDULE_TIME` / `PAIRS` of the `pln-edits` cron job |
| `p2pbot/engine.py` | `p2pbot/` | price engine (base rate, cap clamp, market middle) used by the cron job |
| `p2pbot/rates.py` | `p2pbot/` | `RateStore`: the `/setbase` and `/setcap` values (`var/state.json`) |
| `p2pbot/services.py`, `p2pbot/cli.py`, `p2pbot/edit_queue.py`, `p2pbot/publisher.py`, `p2pbot/uah_config.py`, `p2pbot/telegram/handlers.py`, `p2pbot/telegram/bot.py` | same paths | full versions **before** the trim: `/setbase`, `/setcap`, `/rates`, `/scenarios`, `/scenario`, the scheduler wiring, the `parser` / `rates` / `pln-edits` CLI commands, the engine-driven PLN queue and the cap clamp in `edit_ad` |
| `tests/*` | `tests/` | tests of the code above, as they were |
| `scripts/telegram_stub_smoke.py` | `scripts/` | smoke test of the old command set |

To bring something back, copy the file (or the functions you need) back to its old path
and restore its test from `tests/`. The git history has the same code too.
