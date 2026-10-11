"""Trading universe: liquidity-ranked USDT-M perpetuals, recomputed daily, with a per-symbol
lifecycle (watch_only -> downloading -> verifying -> warming_up -> tradeable -> exited).

See docs/universe.md. Ranking is liquidity only; volatility, trend clarity and volume spikes belong
to the later Market Scanner, which picks opportunities inside this universe.
"""
