"""Symbol universe: which USDT-M perpetuals Deskmind collects data for, and which may be traded.

- ranking.py   liquidity-only membership (median daily quote volume, top N with an exit buffer)
- models.py    lifecycle states and records
- store.py     where states and daily run summaries live (Postgres; in-memory for tests)
- daily.py     the 00:00 UTC job: recompute -> download / verify / warm up entered symbols,
               incrementally update existing members, write the daily summary
"""
