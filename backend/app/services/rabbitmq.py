# app/services/rabbitmq.py
from __future__ import annotations

import json
import asyncio
import uuid
from typing import Any, Dict, Optional

import aio_pika
from aio_pika.robust_connection import RobustConnection
from aio_pika.abc import AbstractRobustChannel, AbstractExchange, ExchangeType
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import settings


class RabbitPublisher:
    """
    生产可用的 AMQP 发布器：
    - Robust 连接（网络抖动自动恢复）
    - Publisher Confirms（消息确认）
    - 声明直连交换器（幂等）
    """
    def __init__(self, amqp_url: Optional[str] = None):
        self._amqp_url = amqp_url or settings.RABBITMQ_AMQP_URL
        self._conn: Optional[RobustConnection] = None
        self._chan: Optional[AbstractRobustChannel] = None
        self._exchange_sync: Optional[AbstractExchange] = None

    async def connect(self) -> None:
        if self._conn and not self._conn.is_closed:
            return
        self._conn = await aio_pika.connect_robust(self._amqp_url, timeout=5.0)
        self._chan = await self._conn.channel(publisher_confirms=True)
        # 声明（幂等）；队列已通过基础设施创建，这里只声明交换器
        self._exchange_sync = await self._chan.declare_exchange(
            settings.RABBITMQ_EXCHANGE_SYNC, ExchangeType.DIRECT, durable=True
        )

    async def close(self) -> None:
        try:
            if self._chan and not self._chan.is_closed:
                await self._chan.close()
        finally:
            if self._conn and not self._conn.is_closed:
                await self._conn.close()
        self._conn = None
        self._chan = None
        self._exchange_sync = None

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=0.2, min=0.2, max=5.0),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def publish_sync(self, routing_key: str, payload: Dict[str, Any], *, message_id: Optional[str] = None, headers: Optional[Dict[str, Any]] = None) -> str:
        """
        向 gmv.sync 交换器发布 JSON 消息。
        - routing_key: 例如 "sync.p1"
        - payload: JSON 可序列化对象
        - 返回 message_id（用于审计）
        """
        if not self._exchange_sync:
            await self.connect()

        mid = message_id or str(uuid.uuid4())
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        hdrs = {"x-gmv": "1", **(headers or {})}

        msg = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=mid,
            headers=hdrs,
        )
        await self._exchange_sync.publish(msg, routing_key=routing_key)
        return mid


# 单例（与 FastAPI 生命周期协调，可在 startup/shutdown 钩子管理）
_publisher: Optional[RabbitPublisher] = None
_pub_lock = asyncio.Lock()

async def get_publisher() -> RabbitPublisher:
    global _publisher
    if _publisher is None:
        async with _pub_lock:
            if _publisher is None:
                _publisher = RabbitPublisher()
                await _publisher.connect()
    return _publisher

async def close_publisher() -> None:
    global _publisher
    if _publisher is not None:
        await _publisher.close()
        _publisher = None

