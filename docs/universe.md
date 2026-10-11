# Trading universe

The universe is the set of USDT-M perpetuals Deskmind may trade. It is **liquidity only**:
eligible contracts (PERPETUAL, TRADING, quote USDT) are ranked by 24h quote volume, and the top
`UNIVERSE_TOP_N` above `UNIVERSE_MIN_QUOTE_VOLUME_USDT` form the universe. A member only leaves
once it ranks below `top_n + UNIVERSE_EXIT_RANK_BUFFER`. Volatility, trend clarity and volume
spikes are NOT used here: they belong to the later hourly Market Scanner, which picks the day's
opportunities inside the universe.

## Daily job (00:00 UTC)
`uv run deskmind universe run` (supervised service `universe`) runs once per UTC date:

1. Recompute the universe from `/fapi/v1/exchangeInfo` + `/fapi/v1/ticker/24hr` (public, no key).
2. Members that left -> `exited`: no new entries, data kept.
3. Entered symbols: missing history is downloaded for `UNIVERSE_HISTORY_DAYS` + the trend agent's
   warm-up (never before the listing day), otherwise updated; verified with the same checks as
   `deskmind data verify` (report `data/reports/universe_verify_{symbol}_{date}.md`); the agents
   are warmed up (trend backfill); only then `tradeable`.
4. Existing tradeable members: incremental update only.
5. The daily summary is stored in `universe_runs.summary` and shown on the dashboard (Universe).

A completed run is never repeated on the same date; a run interrupted by a restart is resumed.
Manual run: `uv run deskmind universe run-once [--force]`. Status: `uv run deskmind universe status`.

## Lifecycle
`watch_only` (listing younger than `UNIVERSE_MIN_LISTING_AGE_DAYS`, default 180: data + chart, no
trading) -> `downloading` -> `verifying` -> `warming_up` -> `tradeable` -> `exited`.
`blocked` (download / verification / warm-up failure, or the disk guard): an alert is raised and
the next run retries. `universe.lifecycle.is_tradeable` / `allows_new_entries` are true ONLY for
`tradeable` symbols whose verification and warm-up completed in the current pipeline pass.

## Safety
- Downloads share the rate-limited market-data clients, `UNIVERSE_DOWNLOAD_CONCURRENCY` symbols at
  a time; each symbol is isolated, so one failure never blocks the rest.
- Disk guard: above `UNIVERSE_DISK_MAX_USED_PCT` (default 90) of the disk holding `DATA_DIR`, all
  further downloads in the run stop and an alert is raised.
- Nothing in the job places, modifies or cancels an order.
