"""共享 Redis 原子配额的客户端契约与可选真实服务验证。"""

import asyncio
import os
import unittest
import uuid

from api.redis_admission import (
    AdmissionDenied, AdmissionUnavailable, acquire, release,
)


class BrokenRedis:
    async def eval(self, *_args):
        raise ConnectionError("redis offline")


class RedisAdmissionUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_denies_admission_and_release(self):
        owner, run = uuid.uuid4(), uuid.uuid4()
        with self.assertRaises(AdmissionUnavailable):
            await acquire(BrokenRedis(), owner, run, 10, 2, 1_000)
        with self.assertRaises(AdmissionUnavailable):
            await release(BrokenRedis(), owner, run)

    async def test_invalid_configuration_never_calls_redis(self):
        owner, run = uuid.uuid4(), uuid.uuid4()
        for rpm, concurrent, lease in ((0, 1, 1000), (1, 0, 1000),
                                       (1, 1, 999), (True, 1, 1000)):
            with self.subTest(params=(rpm, concurrent, lease)), self.assertRaises(ValueError):
                await acquire(BrokenRedis(), owner, run, rpm, concurrent, lease)


@unittest.skipUnless(os.getenv("REDIS_TEST_URL"), "设置一次性 Redis 的 REDIS_TEST_URL 后运行")
class RedisAdmissionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import redis.asyncio as redis
        self.first = redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        self.second = redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        self.owner = uuid.uuid4()

    async def asyncTearDown(self):
        await self.first.aclose()
        await self.second.aclose()

    async def test_two_clients_share_concurrency_and_sliding_rpm(self):
        first_run, second_run, third_run = (uuid.uuid4() for _ in range(3))
        outcomes = await asyncio.gather(
            acquire(self.first, self.owner, first_run, 2, 1, 2_000),
            acquire(self.second, self.owner, second_run, 2, 1, 2_000),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        denied = next(item for item in outcomes if isinstance(item, Exception))
        self.assertIsInstance(denied, AdmissionDenied)
        self.assertEqual(denied.code, "GATEWAY_CONCURRENCY_EXCEEDED")
        winner = first_run if not isinstance(outcomes[0], Exception) else second_run
        loser = second_run if winner == first_run else first_run
        self.assertEqual((await acquire(self.second, self.owner, winner, 2, 1, 2_000)).rpm, 1)
        self.assertTrue(await release(self.first, self.owner, winner))
        self.assertFalse(await release(self.first, self.owner, winner))
        self.assertEqual((await acquire(self.second, self.owner, loser, 2, 1, 2_000)).rpm, 2)
        with self.assertRaises(AdmissionDenied) as rejected:
            await acquire(self.first, self.owner, third_run, 2, 2, 2_000)
        self.assertEqual(rejected.exception.code, "GATEWAY_RPM_EXCEEDED")
        with self.assertRaises(AdmissionDenied) as replay:
            await acquire(self.first, self.owner, winner, 2, 2, 2_000)
        self.assertEqual(replay.exception.code, "GATEWAY_RUN_ALREADY_ADMITTED")

    async def test_expired_lease_releases_capacity(self):
        first_run, second_run = uuid.uuid4(), uuid.uuid4()
        await acquire(self.first, self.owner, first_run, 2, 1, 1_000)
        await asyncio.sleep(1.15)
        result = await acquire(self.second, self.owner, second_run, 2, 1, 1_000)
        self.assertEqual((result.rpm, result.active), (2, 1))
