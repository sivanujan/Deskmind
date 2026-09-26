# Deskmind

Multi-agent advisory system for Binance USDT-M perpetual futures. **Testnet/paper only.**
See [CLAUDE.md](CLAUDE.md) for project rules and the roadmap.

## Prerequisites
- Docker Desktop
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)
- GNU make (optional; the targets are one-liners you can run by hand)

## Setup
```bash
cp .env.example .env        # then set POSTGRES_PASSWORD (and API keys later)
make install                # uv sync
make up                     # checks ports 5435/6380, starts postgres+redis, waits for healthy
make migrate                # alembic upgrade head
make health                 # or: uv run deskmind health
```

## Services
| Service | Image | Host port |
|---|---|---|
| Postgres 17 + TimescaleDB + pgvector | `timescale/timescaledb-ha:pg17` | 127.0.0.1:5435 |
| Redis 7 | `redis:7-alpine` | 127.0.0.1:6380 |

Both services bind to localhost only and use named volumes (`deskmind_pgdata`, `deskmind_redisdata`).
Deskmind never uses the Postgres on port 5432, which belongs to another project.

## Historical data
Symbols come from `DATA_SYMBOLS` in `.env` (default: BTC, ETH, SOL, BNB, XRP perpetuals).
Every command takes `--symbol X` (must be in `DATA_SYMBOLS`) or `--all`.
```bash
uv run deskmind data download --all --start 2023-01-01   # bulk zips + REST
uv run deskmind data update   --all                      # only what is missing
uv run deskmind data verify   --all                      # report: data/reports/verify_{date}.md
uv run deskmind data info     --all
```
To add a symbol: append it to `DATA_SYMBOLS`, then `uv run deskmind data download --symbol NEWUSDT`.

Query with DuckDB, for example the daily close from 5m candles:
```sql
SET TimeZone = 'UTC';
SELECT date_trunc('day', open_time) AS day, arg_max(close, open_time) AS close
FROM read_parquet('data/parquet/ETHUSDT/klines/5m/**/*.parquet', hive_partitioning = false)
GROUP BY 1 ORDER BY 1 DESC LIMIT 7;
```
In Python: `deskmind.data.storage.load_klines(data_dir, "ETHUSDT", "1h", start, end)`.

## Live market data
One WebSocket connection (klines 5m/15m/1h/2h + mark price for every `DATA_SYMBOLS` entry)
keeps Redis current and publishes one `deskmind:bar_close` event per closed timestamp.
```bash
uv run deskmind live run               # foreground service (Ctrl+C to stop); --duration N for tests
uv run deskmind live status            # buffers, staleness, mark/OI age, connection state
uv run deskmind live tail --history 20 # print bar_close events as they arrive
uv run deskmind live verify --hours 1  # live-stored candles vs fresh REST + event sequence check
uv run deskmind live disconnect --seconds 120   # test: drop the running service's WebSocket
```
Agents read candles with `deskmind.data.live.buffers.get_candles(redis, "BTCUSDT", "15m", 200)`.
The Redis key layout is documented in `CLAUDE.md`.

## Web app (chart viewer)
```bash
uv run deskmind api                 # read-only API on http://127.0.0.1:8010 (docs: /docs)
npm --prefix apps/web install       # first time only
npm --prefix apps/web run dev       # http://localhost:3010/chart
```
The chart shows candles + volume for every configured symbol (5m/15m/1h/2h), loads older history
when you scroll left, updates live from the live service, and shows a health badge
(green = live, amber = degraded/stale, red = offline). Overlays drawn by agents (Step 4) follow
the contract in `CLAUDE.md`; in development a sample overlay proves rendering.

## Trend agent
```bash
uv run deskmind agents backfill trend --all --start 2026-06-28   # history -> overlays + reports
uv run deskmind agents run trend                                 # live, next to `live run`
uv run deskmind agents check trend --all                         # no-look-ahead + convergence
```
Rules: `docs/agents/trend.md`. On `/chart`, trend overlays are drawn per timeframe (toggle
swings / BOS-CHoCH / sweeps / trend lines, hover a marker for its rule and times) and
**Replay** shows exactly what the agent knew at any past moment.

## Development
```bash
make test     # pytest (DB/Redis integration tests skip if the containers are down)
make lint     # ruff check + format check
make format   # autofix
```
