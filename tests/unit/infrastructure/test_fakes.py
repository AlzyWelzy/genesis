"""Tests for the in-memory implementations used across the suite.

These fakes are load-bearing test infrastructure: if ``InMemoryCache`` accepts
a value Redis would reject, every test that uses it passes while production
fails. So they are tested against the same constraints as the real thing.
"""

from typing import ClassVar

import pytest

from app.infrastructure.email.client import EmailMessage
from app.infrastructure.email.providers import (
    CollectingEmailProvider,
    build_mime,
    render_template,
)
from app.infrastructure.observability.metrics import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    NoOpMetrics,
    RecordingMetrics,
)
from app.infrastructure.queue.client import InMemoryQueue, Job, TaskRegistry
from app.infrastructure.redis.cache import InMemoryCache


class TestInMemoryCache:
    async def test_round_trip(self, fake_cache: InMemoryCache) -> None:
        await fake_cache.set("k", {"a": 1})
        assert await fake_cache.get("k") == {"a": 1}

    async def test_missing_key_is_a_miss_not_an_error(
        self, fake_cache: InMemoryCache
    ) -> None:
        assert await fake_cache.get("absent") is None

    async def test_rejects_values_redis_could_not_store(
        self, fake_cache: InMemoryCache
    ) -> None:
        """Otherwise a test passes here and the same code fails against Redis."""
        with pytest.raises(TypeError):
            await fake_cache.set("k", {1, 2, 3})

    async def test_expiry_is_honoured(self, fake_cache: InMemoryCache) -> None:
        await fake_cache.set("k", "v", ttl_seconds=0)
        assert await fake_cache.get("k") is None

    async def test_delete_and_exists(self, fake_cache: InMemoryCache) -> None:
        await fake_cache.set("k", 1)
        assert await fake_cache.exists("k") is True
        await fake_cache.delete("k")
        assert await fake_cache.exists("k") is False

    async def test_deleting_a_missing_key_is_not_an_error(
        self, fake_cache: InMemoryCache
    ) -> None:
        await fake_cache.delete("never-existed")

    async def test_clear_prefix(self, fake_cache: InMemoryCache) -> None:
        await fake_cache.set("tenant:1:a", 1)
        await fake_cache.set("tenant:1:b", 2)
        await fake_cache.set("tenant:2:a", 3)

        await fake_cache.clear_prefix("tenant:1")

        assert await fake_cache.get("tenant:1:a") is None
        assert await fake_cache.get("tenant:2:a") == 3

    async def test_stored_values_are_copies(self, fake_cache: InMemoryCache) -> None:
        """Redis round-trips through JSON; mutating a read must not alter the store."""
        await fake_cache.set("k", {"nested": {"n": 1}})

        retrieved = await fake_cache.get("k")
        assert retrieved is not None
        retrieved["nested"]["n"] = 99

        again = await fake_cache.get("k")
        assert again is not None
        assert again["nested"]["n"] == 1


class TestInMemoryQueue:
    async def test_records_enqueued_jobs(self, fake_queue: InMemoryQueue) -> None:
        await fake_queue.enqueue(Job(name="send_email", payload={"to": "a@b.c"}))
        assert len(fake_queue.jobs) == 1
        assert fake_queue.jobs[0].name == "send_email"
        assert fake_queue.jobs[0].payload == {"to": "a@b.c"}

    async def test_enqueue_returns_an_identifier(
        self, fake_queue: InMemoryQueue
    ) -> None:
        assert await fake_queue.enqueue(Job(name="t"))

    async def test_enqueue_many(self, fake_queue: InMemoryQueue) -> None:
        ids = await fake_queue.enqueue_many([Job(name="a"), Job(name="b")])
        assert len(ids) == 2
        assert [j.name for j in fake_queue.jobs] == ["a", "b"]


class TestTaskRegistry:
    def test_register_and_resolve(self) -> None:
        registry = TaskRegistry()

        @registry.register("greet")
        async def greet(payload: dict) -> None:
            pass

        assert registry.resolve("greet") is greet
        assert registry.names == frozenset({"greet"})

    def test_duplicate_registration_is_rejected(self) -> None:
        """A silent overwrite would route jobs to the wrong code."""
        registry = TaskRegistry()

        @registry.register("t")
        async def first(payload: dict) -> None:
            pass

        with pytest.raises(ValueError, match="already registered"):
            registry.register("t")(first)

    def test_unknown_task_raises_with_a_useful_hint(self) -> None:
        registry = TaskRegistry()
        with pytest.raises(KeyError, match="imported by the worker"):
            registry.resolve("nope")


class TestCollectingEmailProvider:
    async def test_captures_instead_of_sending(
        self, fake_email: CollectingEmailProvider
    ) -> None:
        message = EmailMessage(to=["a@b.c"], subject="Hi", template="example")
        await fake_email.send(message)
        assert fake_email.sent == [message]

    async def test_batch_is_captured(self, fake_email: CollectingEmailProvider) -> None:
        await fake_email.send_batch(
            [
                EmailMessage(to=["a@b.c"], subject="1", template="example"),
                EmailMessage(to=["d@e.f"], subject="2", template="example"),
            ]
        )
        assert len(fake_email.sent) == 2


class TestTemplateRendering:
    CONTEXT: ClassVar[dict[str, object]] = {
        "app_name": "Genesis",
        "subject": "Welcome",
        "recipient_name": "Alex",
        "body": "Your workspace is ready.",
        "action_url": "https://example.com/go",
        "action_label": "Open workspace",
    }

    def test_renders_both_parts(self) -> None:
        """A message with no text alternative renders blank in some clients."""
        html, text = render_template("example", self.CONTEXT)
        assert "Alex" in html
        assert "Alex" in text
        assert "<a href" in html
        assert "<a href" not in text

    def test_missing_context_variable_fails_loudly(self) -> None:
        """Silently mailing "Hello ," to a customer is worse than failing."""
        from jinja2 import UndefinedError

        with pytest.raises(UndefinedError):
            render_template("example", {"app_name": "G", "subject": "s"})

    def test_user_data_is_escaped(self) -> None:
        """An unescaped name is a mail-client XSS vector."""
        html, _ = render_template(
            "example", {**self.CONTEXT, "recipient_name": "<script>alert(1)</script>"}
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_mime_puts_text_before_html(self) -> None:
        """multipart/alternative means "last renderable part wins"."""
        html, text = render_template("example", self.CONTEXT)
        message = EmailMessage(to=["a@b.c"], subject="Welcome", template="example")
        mime = build_mime(message, html, text)

        parts = [part.get_content_type() for part in mime.walk()]
        assert parts == ["multipart/alternative", "text/plain", "text/html"]

    def test_mime_headers(self) -> None:
        html, text = render_template("example", self.CONTEXT)
        message = EmailMessage(
            to=["a@b.c", "d@e.f"], subject="Welcome", template="example"
        )
        mime = build_mime(message, html, text)

        assert mime["Subject"] == "Welcome"
        assert mime["To"] == "a@b.c, d@e.f"
        assert "@" in mime["From"]


class TestMetrics:
    def test_noop_discards_everything(self) -> None:
        recorder = NoOpMetrics()
        recorder.increment(HTTP_REQUESTS, method="GET")
        recorder.observe(HTTP_DURATION, 0.1)
        recorder.gauge("anything", 1)

    def test_recording_captures_labels(self) -> None:
        recorder = RecordingMetrics()
        recorder.increment(HTTP_REQUESTS, method="GET", route="/x", status="200")
        recorder.observe(HTTP_DURATION, 0.25, method="GET", route="/x")
        recorder.gauge("queue_depth", 3, queue="default")

        assert recorder.counters == [
            (HTTP_REQUESTS, 1, {"method": "GET", "route": "/x", "status": "200"})
        ]
        assert recorder.observations[0][1] == 0.25
        assert recorder.gauges[0][1] == 3
