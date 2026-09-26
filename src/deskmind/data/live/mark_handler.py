"""Mark price stream ({symbol}@markPrice@1s): latest mark/index price and funding per symbol."""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from deskmind.data.live import buffers as B


class MarkHandler:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.received = 0

    async def on_message(self, data: dict[str, Any]) -> bool:
        """Store the update unless a newer one is already stored (out-of-order safe)."""
        self.received += 1
        doc = {
            "mark_price": data["p"],
            "index_price": data.get("i"),
            "est_settle_price": data.get("P"),
            "funding_rate": data.get("r"),
            "next_funding_time": int(data["T"]) if data.get("T") else None,
            "event_time": int(data["E"]),
            "received_at": B.now_ms(),
        }
        return await B.put_json_if_newer(
            self.redis, B.mark_key(data["s"].upper()), doc, "event_time"
        )
