"""Tests for the broadcast abstraction.

Why these exist
---------------
This module had no tests at all, which for a broadcast channel is a particular
kind of dangerous: both implementations swallow their own failures deliberately,
so a publish that never works produces a ``WARNING`` in a log nobody reads and
is otherwise indistinguishable from success. Nothing downstream raises.

The weight is therefore on the decoding path — where a malformed message must be
skipped rather than kill a long-lived subscriber — and on the Streams cursor,
which is the entire reason to pay for Streams over plain pub/sub. A cursor that
fails to advance turns a consumer into an infinite loop over one entry.

Redis is stubbed rather than real. What is under test is this module's framing,
cursor arithmetic and error handling; Redis participates in none of it, and a
real server would only make the tests slower and flakier.
"""

import json
from typing import Any

import pytest

from app.infrastructure.redis.client import build_key
from app.infrastructure.redis.pubsub import (
    Message,
    RedisPubSub,
    RedisStreamsPubSub,
    _decode,
    _decode_stream_entry,
    _strip_stream_prefix,
)


class StubRedis:
    """Records calls instead of talking to a server."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.added: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.fail = fail

    async def publish(self, channel: str, body: str) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.published.append((channel, body))

    async def xadd(self, key: str, fields: dict[str, Any], **kwargs: Any) -> str:
        if self.fail:
            raise ConnectionError("redis is down")
        self.added.append((key, fields, kwargs))
        return "1-0"


def _install(monkeypatch, stub: object) -> None:
    monkeypatch.setattr(
        "app.infrastructure.redis.pubsub.get_redis", lambda: stub, raising=True
    )


@pytest.fixture
def stub_redis(monkeypatch) -> StubRedis:
    stub = StubRedis()
    _install(monkeypatch, stub)
    return stub


@pytest.fixture
def broken_redis(monkeypatch) -> StubRedis:
    stub = StubRedis(fail=True)
    _install(monkeypatch, stub)
    return stub


def _message(data: str, *, channel: str = "cache") -> Message | None:
    return _decode({"channel": build_key("pubsub", channel), "data": data})


def _require(message: Message | None) -> Message:
    """Assert a message decoded, and hand back the non-optional value."""
    assert message is not None
    return message


class TestPublish:
    async def test_the_message_is_a_json_envelope(self, stub_redis) -> None:
        """JSON, so a consumer written in another language can read it."""
        await RedisPubSub().publish("cache", "invalidate", {"key": "user:1"})

        _, body = stub_redis.published[0]
        assert json.loads(body) == {
            "name": "invalidate",
            "version": 1,
            "payload": {"key": "user:1"},
        }

    async def test_the_channel_is_namespaced(self, stub_redis) -> None:
        """A shared Redis without a namespace is how staging reads production."""
        await RedisPubSub().publish("cache", "invalidate", {})

        channel, _ = stub_redis.published[0]
        assert channel == build_key("pubsub", "cache")

    async def test_the_version_is_carried(self, stub_redis) -> None:
        """A consumer that cannot read v3 needs to be able to tell."""
        await RedisPubSub().publish("cache", "invalidate", {}, version=3)
        assert json.loads(stub_redis.published[0][1])["version"] == 3

    async def test_a_publish_failure_does_not_reach_the_caller(
        self, broken_redis
    ) -> None:
        """A dead broadcast must not fail the request that triggered it.

        The caller did not depend on the outcome; failing its request because a
        fan-out notification did not go out is the wrong trade.
        """
        await RedisPubSub().publish("cache", "invalidate", {})

    async def test_the_failure_is_logged(self, broken_redis, caplog) -> None:
        """Swallowed *and* unlogged would make a dead channel undetectable."""
        with caplog.at_level("WARNING"):
            await RedisPubSub().publish("cache", "invalidate", {})
        assert "Publish failed" in caplog.text


class TestDecoding:
    def test_a_well_formed_message_decodes(self) -> None:
        body = json.dumps({"name": "invalidate", "version": 2, "payload": {"k": 1}})
        assert _message(body) == Message(
            channel="cache", name="invalidate", version=2, payload={"k": 1}
        )

    def test_bytes_from_redis_are_decoded(self) -> None:
        """The pool runs with ``decode_responses=False``, so this is the real shape."""
        raw = {
            "channel": build_key("pubsub", "cache").encode(),
            "data": json.dumps({"name": "invalidate"}).encode(),
        }
        assert _require(_decode(raw)).channel == "cache"

    def test_the_namespace_prefix_is_stripped(self) -> None:
        """A consumer subscribed to "a:b" should not have to know the prefix."""
        assert _require(_message('{"name": "x"}', channel="a")).channel == "a"

    def test_version_defaults_to_one(self) -> None:
        assert _require(_message('{"name": "x"}')).version == 1

    def test_payload_defaults_to_empty(self) -> None:
        assert _require(_message('{"name": "x"}')).payload == {}

    @pytest.mark.parametrize(
        "data",
        [
            "not json at all",
            '{"no_name": true}',  # missing the one required key
            "[]",  # valid JSON, wrong shape
        ],
    )
    def test_an_undecodable_message_is_skipped_not_raised(self, data: str) -> None:
        """One malformed publisher must not stop a long-lived subscriber."""
        assert _message(data) is None


class TestStreamPublish:
    async def test_fields_are_flattened_to_strings(self, stub_redis) -> None:
        """Stream fields are a flat string map; the payload is nested JSON."""
        await RedisStreamsPubSub().publish("audit", "created", {"id": 7}, version=2)

        key, fields, _ = stub_redis.added[0]
        assert key == build_key("stream", "audit")
        assert fields == {"name": "created", "version": "2", "payload": '{"id": 7}'}

    async def test_trimming_is_capped_and_approximate(self, stub_redis) -> None:
        """Exact trimming forces a scan; 10,000 vs 10,050 retained never matters."""
        await RedisStreamsPubSub(maxlen=500).publish("audit", "created", {})

        _, _, kwargs = stub_redis.added[0]
        assert kwargs["maxlen"] == 500
        assert kwargs["approximate"] is True

    async def test_a_publish_failure_is_swallowed(self, broken_redis) -> None:
        await RedisStreamsPubSub().publish("audit", "created", {})


class TestStreamDecoding:
    def test_a_well_formed_entry_decodes(self) -> None:
        fields = {b"name": b"created", b"version": b"2", b"payload": b'{"id": 7}'}
        assert _decode_stream_entry("audit", fields) == Message(
            channel="audit", name="created", version=2, payload={"id": 7}
        )

    def test_defaults_apply_when_optional_fields_are_absent(self) -> None:
        assert _decode_stream_entry("audit", {b"name": b"created"}) == Message(
            channel="audit", name="created", version=1, payload={}
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {b"version": b"1"},  # no name
            {b"name": b"x", b"version": b"not-a-number"},
            {b"name": b"x", b"payload": b"{{{"},
        ],
    )
    def test_an_unusable_entry_is_skipped(self, fields: dict[bytes, bytes]) -> None:
        assert _decode_stream_entry("audit", fields) is None

    def test_the_stream_prefix_is_stripped(self) -> None:
        assert _strip_stream_prefix(build_key("stream", "audit")) == "audit"

    def test_a_bytes_stream_name_is_stripped_too(self) -> None:
        assert _strip_stream_prefix(build_key("stream", "audit").encode()) == "audit"


class TestStreamCursor:
    """The reason to pay for Streams at all: resuming where you stopped."""

    @pytest.fixture
    def reading_redis(self, monkeypatch):
        """A stub whose ``xread`` records the cursor it was asked to read from."""
        key = build_key("stream", "audit")
        batches = [
            [(key, [(b"1-0", {b"name": b"a"}), (b"2-0", {b"name": b"b"})])],
            [(key, [(b"3-0", {b"name": b"c"})])],
        ]

        class ReadingRedis:
            def __init__(self) -> None:
                self.cursors: list[str] = []

            async def xread(self, streams: dict[str, str], **_kwargs: Any):
                self.cursors.append(streams[key])
                return batches.pop(0) if batches else []

        stub = ReadingRedis()
        _install(monkeypatch, stub)
        return stub

    async def test_the_cursor_starts_at_new_messages_only(self, reading_redis) -> None:
        """``$`` matches plain pub/sub semantics, which is the safe default."""
        subscription = RedisStreamsPubSub().subscribe("audit")
        await anext(subscription)
        await subscription.aclose()

        assert reading_redis.cursors[0] == "$"

    async def test_the_cursor_advances_past_consumed_entries(
        self, reading_redis
    ) -> None:
        """Without this, a consumer re-reads the same entry forever."""
        subscription = RedisStreamsPubSub().subscribe("audit")
        collected = [await anext(subscription) for _ in range(3)]
        await subscription.aclose()

        assert [(entry, message.name) for entry, message in collected] == [
            ("1-0", "a"),
            ("2-0", "b"),
            ("3-0", "c"),
        ]
        # The second read resumes after the last entry of the first batch.
        assert reading_redis.cursors[1] == "2-0"

    async def test_replay_from_the_beginning_is_possible(self, reading_redis) -> None:
        """``"0"`` is what makes a rebuild-from-the-log recovery possible."""
        subscription = RedisStreamsPubSub().subscribe("audit", last_id="0")
        await anext(subscription)
        await subscription.aclose()

        assert reading_redis.cursors[0] == "0"
