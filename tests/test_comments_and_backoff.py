import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Bot.poller.main import process_post_data, set_canny_rate_limit_backoff, check_canny_backoff
from Bot.shared.canny import fetch_canny_api, fetch_canny_data

class MockValkey:
    def __init__(self):
        self.store = {}
        self.sets = {}
        self.hashes = {}
        self.lists = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = str(value)
        return True

    async def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    async def hset(self, name, key=None, value=None, mapping=None):
        if name not in self.hashes:
            self.hashes[name] = {}
        if mapping:
            self.hashes[name].update(mapping)
        elif key is not None:
            self.hashes[name][key] = str(value)
        return True

    async def sismember(self, name, value):
        return value in self.sets.get(name, set())

    async def sadd(self, name, value):
        if name not in self.sets:
            self.sets[name] = set()
        self.sets[name].add(value)
        return 1

    async def zincrby(self, name, amount, value):
        return True

    async def expire(self, name, time_sec):
        return True

    async def exists(self, key):
        return key in self.store or key in self.sets or key in self.hashes

    async def lpush(self, name, value):
        if name not in self.lists:
            self.lists[name] = []
        self.lists[name].insert(0, value)
        return len(self.lists[name])

    async def incr(self, key):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    async def hincrby(self, name, key, amount):
        if name not in self.hashes:
            self.hashes[name] = {}
        val = int(self.hashes[name].get(key, 0)) + amount
        self.hashes[name][key] = str(val)
        return val


@pytest.mark.asyncio
async def test_comment_notification_enqueuing():
    valkey = MockValkey()
    pid = "post_123"
    p_url = "https://feedback.vrchat.com/feature-requests/p/test-post"
    board_info = {"name": "Feature Requests", "urlName": "feature-requests"}

    old_post = {
        "_id": pid,
        "title": "Test Post",
        "score": 10,
        "status": "open",
        "commentCount": 2,
        "created": "2025-01-01T00:00:00.000Z"
    }
    await valkey.set(f"post_cache:{pid}", json.dumps(old_post))

    updated_post = {
        "_id": pid,
        "title": "Test Post",
        "score": 10,
        "status": "open",
        "commentCount": 5,
        "created": "2025-01-01T00:00:00.000Z"
    }

    # Process updated post with new comments
    await process_post_data(valkey, updated_post, board_info, p_url, "test-post")

    # Check job queue
    assert "{discord_jobs}" in valkey.lists
    job_raw = valkey.lists["{discord_jobs}"][0]
    job = json.loads(job_raw)

    assert job["type"] == "comment"
    assert job["old_comments"] == 2
    assert job["comments"] == 5
    assert job["url"] == p_url
    assert await valkey.get(f"notified_comments:{pid}") == "5"


@pytest.mark.asyncio
async def test_canny_rate_limit_429_and_403():
    class MockResponse:
        def __init__(self, status):
            self.status = status

    class MockContextManager:
        def __init__(self, response):
            self.response = response
        async def __aenter__(self):
            return self.response
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session = MagicMock()

    # Test 429 in fetch_canny_api
    mock_session.post.return_value = MockContextManager(MockResponse(429))
    with patch("Bot.shared.canny.get_session", new_callable=AsyncMock, return_value=mock_session):
        res_429 = await fetch_canny_api("/api/posts/get", {})
        assert res_429 == {"error": "rate_limit"}

    # Test 403 in fetch_canny_api
    mock_session.post.return_value = MockContextManager(MockResponse(403))
    with patch("Bot.shared.canny.get_session", new_callable=AsyncMock, return_value=mock_session):
        res_403 = await fetch_canny_api("/api/posts/get", {})
        assert res_403 == {"error": "rate_limit"}

    # Test 429 in fetch_canny_data
    mock_session.get.return_value = MockContextManager(MockResponse(429))
    with patch("Bot.shared.canny.get_session", new_callable=AsyncMock, return_value=mock_session):
        res_data_429 = await fetch_canny_data("https://feedback.vrchat.com/test")
        assert res_data_429 == {"error": "rate_limit"}

    # Test 403 in fetch_canny_data
    mock_session.get.return_value = MockContextManager(MockResponse(403))
    with patch("Bot.shared.canny.get_session", new_callable=AsyncMock, return_value=mock_session):
        res_data_403 = await fetch_canny_data("https://feedback.vrchat.com/test")
        assert res_data_403 == {"error": "rate_limit"}


@pytest.mark.asyncio
async def test_valkey_rate_limit_backoff_timer():
    valkey = MockValkey()
    await set_canny_rate_limit_backoff(valkey, 3600)

    backoff_until = await valkey.get("canny_rate_limit_backoff")
    assert backoff_until is not None
    rem = float(backoff_until) - time.time()
    assert 3500 <= rem <= 3600

    # Test check_canny_backoff triggers sleep
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await check_canny_backoff(valkey)
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] > 0
