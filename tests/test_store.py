"""存储层：Redis 与内存实现同一接口，同一组测试各跑一遍。"""

import asyncio

import pytest

from datapilot.store import MemoryStore, RedisStore, make_store

REDIS_URL = "redis://localhost:6379/15"  # 测试专用库，跑前清空


def _redis_available() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping()
        return True
    except Exception:
        return False


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    if not _redis_available():
        pytest.skip("本机没有运行 Redis")
    import redis

    redis.Redis.from_url(REDIS_URL).flushdb()
    return RedisStore(REDIS_URL)


async def test_set_get_roundtrip(store):
    await store.set_json("dp:a", {"x": [1, "中文"]}, ttl_s=60)
    assert await store.get_json("dp:a") == {"x": [1, "中文"]}
    assert await store.get_json("dp:missing") is None


async def test_ttl_expires(store):
    await store.set_json("dp:t", 1, ttl_s=1)
    await asyncio.sleep(1.2)
    assert await store.get_json("dp:t") is None


async def test_delete_prefix(store):
    await store.set_json("dp:cache:financial:1", 1, ttl_s=60)
    await store.set_json("dp:cache:financial:2", 2, ttl_s=60)
    await store.set_json("dp:cache:superhero:1", 3, ttl_s=60)
    assert await store.delete_prefix("dp:cache:financial:") == 2
    assert await store.get_json("dp:cache:financial:1") is None
    assert await store.get_json("dp:cache:superhero:1") == 3


async def test_memory_store_evicts_oldest_when_full():
    s = MemoryStore(max_items=2)
    await s.set_json("a", 1, ttl_s=60)
    await s.set_json("b", 2, ttl_s=60)
    await s.get_json("a")  # a 最近用过
    await s.set_json("c", 3, ttl_s=60)
    assert await s.get_json("b") is None and await s.get_json("a") == 1


async def test_make_store_falls_back_to_memory_when_redis_down(caplog):
    s = await make_store("redis://localhost:1/0")  # 端口 1 必然连不上
    assert isinstance(s, MemoryStore)
    assert "退回内存" in caplog.text


async def test_make_store_uses_redis_when_up():
    if not _redis_available():
        pytest.skip("本机没有运行 Redis")
    assert isinstance(await make_store(REDIS_URL), RedisStore)


async def test_incr_counts_within_window_and_expires(store):
    assert await store.incr("dp:rl:u1:0", ttl_s=1) == 1
    assert await store.incr("dp:rl:u1:0", ttl_s=1) == 2
    assert await store.incr("dp:rl:u2:0", ttl_s=1) == 1  # 不同 key 各自计数
    await asyncio.sleep(1.2)
    assert await store.incr("dp:rl:u1:0", ttl_s=1) == 1  # 窗口过期后重新计数


async def test_incr_is_atomic_under_concurrency(store):
    counts = await asyncio.gather(*(store.incr("dp:rl:c:0", ttl_s=60) for _ in range(50)))
    assert sorted(counts) == list(range(1, 51))


async def test_memory_counters_do_not_evict_sessions():
    s = MemoryStore(max_items=2)
    await s.set_json("dp:session:a:s1", {"turns": []}, ttl_s=60)
    for i in range(5):
        await s.incr(f"dp:rl:u{i}:0", ttl_s=60)
    assert await s.get_json("dp:session:a:s1") == {"turns": []}
