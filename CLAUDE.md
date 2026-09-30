# Deskmind — project rules

## Purpose
Deskmind is a multi-agent advisory system for crypto futures trading on Binance USDT-M perpetuals.
Specialist analyst agents (mostly deterministic Python) feed a Head agent (LLM via OpenRouter,
OpenAI-compatible API; model set by `OPENROUTER_MODEL`, default `deepseek/deepseek-v4.1-flash`,
always a pinned version ID, never a `~...-latest` alias).
A Risk Manager (deterministic code, has veto power) sizes every trade, and a human approves before
anything is executed. Phase 1 MVP: BTCUSDT only, one strategy, Binance TESTNET only, end-to-end.

## HARD CONSTRAINTS
1. There is an existing PostgreSQL server on 127.0.0.1:5432 used by ANOTHER project.
   NEVER connect to it, modify it, create databases in it, install extensions on it, or change its
   config. Do not touch port 5432 at all. (`config.py` refuses POSTGRES_HOST_PORT=5432.)
2. Deskmind has its OWN PostgreSQL + TimescaleDB + pgvector in Docker
   (`timescale/timescaledb-ha:pg17`), database `deskmind`, host port **5435**.
   (The spec asked for 5433, but 5433 is held by another native Postgres and 5434 by the bijoux
   project, so 5435 was chosen. Do not use 5432, 5433 or 5434.)
3. Deskmind has its OWN Redis in Docker on host port **6380** (never 6379).
4. Before starting containers, check that Deskmind's ports are free (`make up` runs
   `scripts/check_ports.py`). If a port is held by something else, stop and tell the user.
5. No secrets in code or git. Secrets live in `.env` (gitignored); `.env.example` has placeholders.
6. TRADING_MODE must be `paper` or `testnet`. Anything else refuses to start. There is no live mode.
   Do not add one without an explicit instruction from the user.
7. Binance keys are `BINANCE_API_KEY` / `BINANCE_SECRET_KEY`. They are only ever sent to
   `BINANCE_FUTURES_BASE_URL`, which `config.py` restricts to testnet hosts
   (`testnet.binancefuture.com`, `demo-fapi.binance.com`). Never hard-code or add a mainnet URL
   (`fapi.binance.com`), and never print or log key values.

## Trading-safety rules
- Risk rules, leverage caps and validation/holdout data are NEVER changed by agents. Only the human
  changes them, by hand, in reviewed commits.
- Money-touching logic (risk, sizing, execution) is deterministic code with unit tests. It is never
  taken from LLM output. LLMs propose; code validates, sizes and can veto.
- Every position has an exchange-side stop-loss (STOP_MARKET / reduce-only on Binance). Never rely on
  a client-side stop.
- Every order carries a unique `client_order_id`, which is also stored in `trade_journal` (unique
  constraint), so orders are idempotent and can be reconciled.
- Decisions are made on candle close only. Never act on a forming candle.
- Backtests must have no look-ahead: at bar t, use only data available at t's close; signals fill at
  the next bar at the earliest; indicators must not use future data; keep holdout data separate.
- Prices and sizes use `Decimal` / `NUMERIC`, never float.
- A human approves every trade before execution.

## Stack and conventions
- Python 3.12, uv, asyncio throughout, pydantic-settings, SQLAlchemy 2 async (asyncpg) + Alembic,
  redis-py async, structlog JSON logs, pytest + pytest-asyncio, ruff.
- Historical candles: Parquet under `data/parquet/` (gitignored) + DuckDB.
- Layout: `src/deskmind/{config,logging,health,cli}.py`, `db/`, `cache/`, `data/`, `agents/`,
  `risk/`, `execution/`, `backtest/`; migrations in `migrations/versions/`.
- Schema changes always go through a new Alembic migration; never edit an applied migration.
- Before calling work done: `make lint` and `make test` must pass.

## Historical data rules (Step 2)
- Symbols come only from config (`DATA_SYMBOLS`, `TRADE_SYMBOLS` in `.env`; defaults in
  `config.py`) and the liquidity universe table. Never hardcode a symbol anywhere else; every
  function takes `symbol`.
  Adding a symbol = add it to `DATA_SYMBOLS` + run `deskmind data download --symbol X`.
  A test (`test_no_symbol_hardcoded_in_source`) enforces this.
- Source of truth for backtests: `data/parquet/{symbol}/...` (klines 5m/15m/1h/2h, mark-price
  klines 5m/1h, funding), built by `deskmind data download|update`. Read candles with
  `storage.load_klines(data_dir, symbol, tf, start, end)`. Exchange filters snapshots live
  in `data/meta/{symbol}_filters_{date}.json` (strings; use them for tickSize/stepSize rounding).
- Parquet prices/volumes are float64 for analytics only. Execution, risk and sizing code must use
  `Decimal` and the exchange filters, never these floats.
- Market data comes from live Binance public endpoints (`BINANCE_MARKET_DATA_URL`, default
  `https://fapi.binance.com`) with the unauthenticated `data.http_client.RateLimitedClient`. Never
  add API keys or signing to that client; orders stay on testnet.
- Every bulk zip is checked against its `.CHECKSUM`. Never use unverified zips.
- Gaps are reported, never filled or interpolated. The currently forming candle is never stored.
- Reconcile (runs after every download/update): the bulk archive contains flat zero-trade
  placeholder candles during some incidents, so zero-trade klines and cross-timeframe mismatches
  are re-downloaded from REST, and REST wins. Mismatches REST confirms are Binance's own data and
  are recorded in each symbol's `_manifest.json` (`reconcile.cross_tf_confirmed`). See the latest
  `data/reports/verify_*.md` for the per-symbol list (e.g. BTCUSDT klines around 2023-11-10
  15:00-16:40 and 2024-10-28 20:15-21:10 UTC: the exchange's 15m/1h/2h candles are truncated,
  the 5m candles are complete). Backtests on higher timeframes should resample from 5m, or at
  least be aware of these buckets.
- Mark vs last price divergence (>1% on close) is report-only (INFO): real market behaviour
  (liquidation cascades, wicks), not bad data. Liquidation simulation must use mark price.
- Funding interval changes announced by the exchange (bulk `funding_interval_hours`) are valid.
- A gap present on several symbols at the same time is exchange-wide (the report says so).
- `deskmind data verify` must show no FAIL before data is used for a backtest.

## Liquidity universe + daily job
- `src/deskmind/universe/`: membership is LIQUIDITY ONLY (median daily quote volume of the last
  30 closed days; enter at rank <= UNIVERSE_TOP_N=30, stay while rank <= UNIVERSE_EXIT_RANK=40).
  Never add volatility, trend clarity or volume spikes to universe ranking: those belong to the
  later hourly Market Scanner, which picks opportunities INSIDE the universe.
- Symbol sources: `DATA_SYMBOLS` (configured) + universe members (table `universe_symbols`). New
  entries additionally need `TRADE_SYMBOLS` (Phase 1: BTCUSDT) AND universe state `tradeable`
  (`universe.daily.can_open_new_entry`, checked by the trade service; exits are never gated).
- Lifecycle: watch_only (listed < 180 days: data + chart, no trading) / downloading -> verifying
  -> warming_up -> tradeable; blocked (download/verify/warm-up failed or disk guard, alert, retried
  next run); exited (no new entries, data kept, never deleted).
- Point-in-time history: `universe_membership` (day, symbol, rank, tradeable) +
  `universe_delistings` (migration 0008). Day D is decided from candles closed before D. Built by
  `deskmind universe backfill-history --start ...` from data.binance.vision (includes DELISTED
  symbols) and extended by the daily job. Backtests: `MembershipHistory.get_universe(D)` gates
  entries (a missing day = nobody); an open position on a delisted symbol is settled at the last
  traded price (exit_reason `delisted`). `backtest run --universe auto|require|off`; the run
  registry records `universe_filter`.
- Chart selector (`/api/symbols` entries) = DATA_SYMBOLS + current members + watch_only, with
  state and `has_data`; the trend agent follows the same live symbol set as the live service.
- Daily job: `deskmind universe scheduler` (supervisor service "universe", 00:01 UTC, not started
  automatically) or `deskmind universe run` once; `deskmind universe status`. Summaries go to
  `universe_runs` and the dashboard Universe page (Telegram later). Downloads: at most
  UNIVERSE_DOWNLOAD_CONCURRENCY at once through the weight-limited client, one failure never
  blocks the rest, disk guard stops NEW downloads when free space < UNIVERSE_DISK_MIN_FREE_GB (updates continue).

## Live market data rules (Step 3)
- `deskmind live run` (src/deskmind/data/live/) keeps Redis current from combined WebSockets
  (`wss://fstream.binance.com/market/stream?streams=...`; kline + markPrice streams live on the
  `/market` route, unrouted URLs no longer deliver them). Symbols = DATA_SYMBOLS + universe
  tradeable/watch_only, re-read every 60 s (`live/universe_sync.py`): at most
  LIVE_MAX_STREAMS_PER_CONNECTION streams per connection (Binance max 1024), a symbol's streams
  always share one connection, assignment is stable and only connections whose symbol list
  changed are restarted (their normal connect path warms buffers + backfills gaps from REST).
  Connection 0 reports on the main health hash, others on `...:health:service:{n}`. Stream names are case-sensitive after
  the symbol (`btcusdt@markPrice@1s`). Public data only, no API key.
- Agents read candles ONLY via `deskmind.data.live.buffers.get_candles(redis, symbol, tf, n)`
  (closed candles, Decimal values) and decide ONLY on `deskmind:bar_close` events. The open
  (partial) candle is for display and must never drive a decision.
- One bar_close event per (symbol, close_time), in time order, only after every timeframe that
  closes at that time has arrived (10:00 -> 5m,15m,1h,2h). Missing after 10s -> REST backfill;
  still missing -> event with `degraded: true` + `missing`. A REST/network error never produces a
  degraded event; the boundary waits. After reconnects, gaps are backfilled from REST first.
- Closed candles are upserted into the Postgres `candles` hypertable (idempotent). Parquet is
  only written by `deskmind data update`, never by the live service.
- Redis layout, schema version 1 (bump `SCHEMA_VERSION` in buffers.py on any change):
  | key | type | content |
  |---|---|---|
  | `deskmind:schema_version` | string | "1" |
  | `deskmind:v1:candles:{SYM}:{tf}` | zset | last 500 closed candles, score=open_time, member = compact JSON `[open_time,o,h,l,c,v,close_time,quote_v,trades,taker_buy_v,taker_buy_quote_v]` (Binance strings, exact) |
  | `deskmind:v1:open:{SYM}:{tf}` | string | `{"is_open": true, "k": [...], "event_time", "updated_at"}` |
  | `deskmind:v1:mark:{SYM}` | string | mark_price, index_price, est_settle_price, funding_rate, next_funding_time, event_time, received_at |
  | `deskmind:v1:oi:{SYM}` | string | open_interest, time, received_at (REST poll every 5 min) |
  | `deskmind:v1:health:streams` | hash | stream -> last message ms |
  | `deskmind:v1:health:service` | hash | state, ws_state, heartbeat, reconnects, clock_drift_ms, counters |
  | `deskmind:v1:bar_close:last:{SYM}` | string | last emitted close_time (restart resume) |
  | `deskmind:v1:bar_close:sent:{SYM}:{t}` | string | SET NX dedupe marker, 7 day TTL |
  | `deskmind:v1:bar_close:log` | stream | every emitted event (capped ~20k) |
  | `deskmind:v1:control:ws_block_until` | string | test hook used by `live disconnect` |
  | channel `deskmind:bar_close` | pub/sub | `{symbol, close_time, close_time_iso, closed_timeframes, degraded, missing, schema_version, emitted_at}` |
- Reliability: reconnect with backoff, rotate before Binance's 24h limit, stale stream -> health
  degraded (kline 30s, mark 10s), clock drift vs Binance checked at start + hourly (warn > 1s).

## Web app + read-only API
- API: `src/deskmind/api/` (FastAPI), `uv run deskmind api` -> http://127.0.0.1:8010 (docs at
  /docs). Binds 127.0.0.1 only. GET endpoints + one WebSocket, NO endpoint that places, modifies or
  cancels orders (a test enforces GET-only). Never writes to Postgres, Redis or Parquet.
  CORS/WebSocket origins: `WEB_ORIGINS` (default http://localhost:3010, http://127.0.0.1:3010).
- Web: `apps/web/` (Next.js 16 App Router, TypeScript, Tailwind v4, Lightweight Charts 5),
  `npm --prefix apps/web run dev` -> http://localhost:3010/chart. Next 16 differs from older
  versions: read `apps/web/node_modules/next/dist/docs/` before changing it (`next lint` is gone,
  use `npm run lint`; `searchParams` are async; `middleware` is now `proxy`).
  Keep the TradingView attribution (logo + footer link) required by Lightweight Charts.
- Dashboard login (`api/auth.py`, `DASHBOARD_PASSWORD_HASH`; set it with
  `uv run python scripts/set_dashboard_password.py`). Control actions are POSTs that need a session
  + CSRF token; none of them places, modifies or cancels an order.

## Dashboard truth rule
- The dashboard shows ONLY data from a backend endpoint that reads the real source of truth. No
  mock data, placeholder numbers or UI-side defaults (`?? "normal"`, `?? 50000`). Missing data is an
  explicit empty state ("No data yet", "Not built yet — Step 11") or `null` + `reason` from the API.
- Truth endpoints (`src/deskmind/api/dashboard.py`): `/api/dashboard/agents` (agents/registry.py +
  Redis health + agent_reports), `/api/dashboard/strategies` (strategy module docstrings,
  `default_params`, backtest registry), `/api/dashboard/risk` (RiskConfig, hard caps, the comments
  in risk/config.py, equity source per mode, breaker), `/api/dashboard/account` (read-only mainnet
  snapshot, never used for sizing), `/api/dashboard/backtest-meta` (BACKTEST_SPLITS, registry).
- Adding an agent = add it to `agents/registry.py`; planned agents go in `PLANNED`.
- `tests/test_dashboard_truth.py` fails on hardcoded risk limits, agent names/versions, strategy
  names/params or mock fallbacks in `apps/web/src`, on frontend calls to endpoints that do not
  exist, and when the truth endpoints drift from their sources.

## Read-only mainnet key + my Binance history
- `data/mainnet_readonly.py` is the ONLY code that talks to mainnet with a key
  (`BINANCE_MAINNET_READONLY_API_KEY/SECRET`). Double lock: GET only + `ALLOWLISTED_ENDPOINTS`
  checked before any network call, and `check_permissions()` refuses keys with trading, withdraw,
  transfer or margin permission. Its balance is display-only; the Risk Manager never sizes from it.
- A futures refusal becomes "Needs futures read access — not enabled (by design)". Never suggest
  enabling futures (or anything else) on that key.
- The user's own fills go to `external_trades` (migration 0006), never `trade_journal`. Dedupe:
  unique (source, market, symbol, trade_id); CSV rows without an id get a content hash.
  `uv run deskmind account import-history [--since YYYY-MM-DD]` (API, futures ~3 months) and
  `uv run deskmind account import-csv FILE [--market spot|futures]` (Binance export), or upload on
  Trades & Journal. The chart overlays them as source `binance:history`.

## Overlay contract (schema version 1) — what agents draw on the chart
- Source of truth: `src/deskmind/api/overlays.py` (Pydantic). Mirrors: TypeScript
  `apps/web/src/lib/overlays.ts`, JSON Schema `docs/overlay.schema.json` (a test fails if it drifts;
  regenerate with `uv run python -m deskmind.api.overlays > docs/overlay.schema.json`).
- Served by `GET /api/overlays?symbol&tf&start&end` as `{symbol, timeframe, schema_version, overlays}`
  (empty until Step 4). Times = UNIX seconds UTC, prices = display floats.
- Common fields: `id`, `type` (marker|line|zone), `kind` (semantic: swing_high, bos, choch,
  trendline, order_block, ...), `source_agent`, `agent_version`, `schema_version`.
  - marker: `time, position (above|below), shape (circle|square|arrowUp|arrowDown), color, text`
  - line:   `start_time, start_price, end_time, end_price, style (solid|dashed|dotted), color, label`
  - zone:   `start_time, end_time, top, bottom, color (#rrggbbaa for fill), label`
- The chart toggles overlays per `source_agent`. Breaking change -> new schema_version on both sides.

## Analyst agents (Step 4, 8, 9)
- Agents are deterministic Python in `src/deskmind/agents/` (no LLM). The rule spec is the source
  of truth: `docs/agents/<agent>.md` (trend: `docs/agents/trend.md`). Code and spec change together.
- NO LOOK-AHEAD: engines are incremental (`on_candle` with closed candles only, in time order).
  Every item carries `pivot_time` (drawn) and `confirmed_at` (knowable); state as of T uses only
  items with `confirmed_at <= T` (`models.as_of`). One code path for live, backfill and tests;
  never add a vectorised shortcut. `deskmind agents check trend --all` must PASS after any change.
- All parameters live in the versioned config (`agents/trend/config.py`); ANY parameter or rule
  change bumps `AGENT_VERSION` and gets a line in the spec's version history. Stored items
  (`agent_events`) and reports (`agent_reports`) are keyed by version; the API serves only the
  current version, so re-run `deskmind agents backfill trend --all --start ...` after a bump.
- Live: `deskmind agents run trend` (separate process; subscribes to `deskmind:bar_close`, reads
  Redis buffers, writes `agent_events` + `agent_reports`, publishes `deskmind:agent_report`).
- Overlays: `GET /api/overlays?symbol&tf&start&end&as_of` (as_of = replay), report:
  `GET /api/agents/trend/report?symbol&as_of`. The chart's Replay mode uses both.
- Agent storage (ict_smc since 1.2.0; do the same for new agents): items + every state
  transition are the point-in-time source of truth (`agent_item_transitions`, `ict_smc_items`,
  compressed TimescaleDB hypertables). NEVER store a full report per bar for backfills: state as
  of T is rebuilt (`agents/ict_smc/history.reconstruct_state`, tested equal to the live engine).
  Full reports only for live runs, kept ICT_SMC_REPORT_RETENTION_DAYS (default 7).
  Old-version reports/items are deleted only after the user confirms.

## Commands
`make up | down | logs | ps | migrate | test | lint | format | health`, and `uv run deskmind health`.
Data (each takes `--symbol X` or `--all`): `uv run deskmind data download --all --start 2023-01-01`,
`data update --all`, `data verify --all` (writes `data/reports/verify_{date}.md`), `data info --all`.
Live: `uv run deskmind live run | status | tail | verify --hours N | disconnect --seconds N`.
Universe: `uv run deskmind universe run | status | scheduler | backfill-history --start D`.
Agents: `uv run deskmind agents run trend | backfill trend --all --start YYYY-MM-DD | check trend --all`.
API + web: `uv run deskmind api` (:8010) and `npm --prefix apps/web run dev` (:3010);
web checks: `npm --prefix apps/web run lint` and `npm --prefix apps/web run typecheck`.
Network tests: `uv run pytest -m network`.

## Roadmap
### Phase 1 (done)
1. **Foundation**: repo, Docker (Postgres/Timescale/pgvector :5435, Redis :6380), config, logging,
   DB schema, health CLI, tests. *(done)*
2. **Historical data**: klines, mark-price klines and funding for all `DATA_SYMBOLS` (BTC, ETH,
   SOL, BNB, XRP) into Parquet/DuckDB, checksum-verified, reconciled against REST, with a
   multi-symbol data-quality report. *(done)*
3. **Live data**: combined WebSocket (klines 5m/15m/1h/2h + mark price) -> Redis buffers + Postgres,
   OI polling, one bar_close event per closed timestamp, reconnect/backfill, health. *(done)*
4. **Trend agent**: trend & structure agent (swings, HH/HL/LH/LL, BOS/CHoCH, sweeps, trend lines),
   no look-ahead, live + backfill, chart overlays and Replay mode. *(done)*
5. **Backtest engine**: event-driven backtester with no look-ahead, fees/slippage/funding, split
   registry (train / validation / locked test), strategy `trend_follow_v1`. *(done)*
6. **Risk Manager**: deterministic sizing, leverage caps, stop bounds, circuit breakers, veto, with
   exhaustive unit tests. *(done)*
7. **Execution (paper/testnet)**: order placement with exchange-side SL/TP and client_order_id,
   reconciliation, trade journal, human approval gate. *(done)*
- Plus: control **Dashboard** (login, truth endpoints, no fake data) and **Binance history import**
  (read-only key + CSV into `external_trades`). *(done)*

### Next
8. **ICT/SMC Liquidity agent**: liquidity pools (BSL/SSL), order blocks, fair value gaps (FVG),
   premium/discount, OTE.
9. **ICT Time agent** (sessions, killzones, PDH/PDL, PWH/PWL) + **Price Action agent** (S/R,
   patterns).
10. **Confluence engine** (deterministic multi-timeframe alignment + scoring) + strategy research
    on TRAIN, confirmation on VALIDATION, every run recorded in the backtest registry.
11. **Head agent** (LLM via OpenRouter) + Telegram advisories: explains and proposes, never sizes
    (the Risk Manager sizes and can veto; a human approves).
12. **Server deployment + long runs**: demo-key verification, stress test, fault injection.

Later: learning loop (Mistake Analyzer, lessons), Research agent, Derivatives, Order flow and News
agents.

**Why this order:** the Head agent combines several analyst reports, so it comes after several
analyst agents (and the confluence engine) exist. The learning loop needs real trades and a
strategy with a proven edge, so it comes last.
