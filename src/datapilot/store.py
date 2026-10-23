"""键值存储：会话与查询缓存共用。Redis 优先，连不上时自动退回内存。

两种实现同一接口（get_json / set_json / delete_prefix / incr），值按 JSON 存，都带过期时间。
- Redis：服务重启不丢，多个后端进程共享
- 内存：别人拿到代码包不装 Redis 也能跑；带 LRU 上限，防止无限增长
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Store(Protocol):
    name: str

    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def incr(self, key: str, ttl_s: int) -> int:
        """计数加一并返回新值；key 第一次出现时设过期时间，之后不续期（固定窗口限流用）。"""
        ...


class MemoryStore:
    name = "memory"

    def __init__(self, max_items: int = 10_000):
        self.max_items = max_items
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        # 计数单独存放：限流 key 每个用户每分钟一个，混在一起会把会话和缓存挤出 LRU
        self._counters: OrderedDict[str, tuple[float, int]] = OrderedDict()

    async def get_json(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, raw = item
        if time.monotonic() > expires:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        self._data[key] = (time.monotonic() + ttl_s, json.dumps(value, ensure_ascii=False))
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            del self._data[k]
        return len(keys)

    async def incr(self, key: str, ttl_s: int) -> int:
        # 单线程事件循环里没有 await 打断，读改写天然原子
        item = self._counters.get(key)
        now = time.monotonic()
        if item is None or now > item[0]:
            expires, n = now + ttl_s, 1
        else:
            expires, n = item[0], item[1] + 1
        self._counters[key] = (expires, n)
        self._counters.move_to_end(key)
        while len(self._counters) > self.max_items:
            self._counters.popitem(last=False)
        return n


class RedisStore:
    name = "redis"

    def __init__(self, url: str):
        import redis.asyncio as aioredis

        self.url = url
        self.client = aioredis.Redis.from_url(url, decode_responses=True)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.client.get(key)
        return None if raw is None else json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        await self.client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_s)

    async def delete_prefix(self, prefix: str) -> int:
        # SCAN 而不是 KEYS：KEYS 会阻塞 Redis
        n = 0
        async for key in self.client.scan_iter(match=f"{prefix}*", count=500):
            n += await self.client.delete(key)
        return n

    async def incr(self, key: str, ttl_s: int) -> int:
        # MULTI 事务：INCR 与 EXPIRE NX 一起提交，不会出现计数了却没设过期的 key
        async with self.client.pipeline(transaction=True) as pipe:
            n, _ = await pipe.incr(key).expire(key, ttl_s, nx=True).execute()
        return n


async def make_store(url: str | None) -> Store:
    """启动时探测 Redis：连得上就用，连不上打印日志并退回内存，服务照常启动。"""
    if url:
        try:
            store = RedisStore(url)
            await store.client.ping()
            log.info("存储：Redis %s", url)
            return store
        except Exception as e:
            log.warning("连不上 Redis（%s），退回内存存储：%s", url, e)
    return MemoryStore()
