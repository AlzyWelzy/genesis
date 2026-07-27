"""Tests for the pure utility helpers.

Grouped in one module because each helper needs only a handful of cases; the
class names keep them navigable. The security-relevant helpers — filename
sanitisation, content sniffing, redaction, constant-time comparison — carry the
most cases, because those are the ones where a gap is a vulnerability rather
than a cosmetic bug.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.common.constants import REDACTED
from app.common.utils import collections as cols
from app.common.utils import crypto, files, strings
from app.common.utils.datetime import (
    add_business_days,
    end_of_day,
    ensure_utc,
    from_iso,
    humanize_timedelta,
    start_of_day,
    to_iso,
    utc_now,
)


class TestDatetime:
    def test_utc_now_is_aware_and_utc(self) -> None:
        now = utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_ensure_utc_assumes_naive_is_utc(self) -> None:
        naive = datetime(2026, 1, 15, 10, 30)  # noqa: DTZ001 - naive on purpose
        assert ensure_utc(naive).tzinfo is UTC

    def test_ensure_utc_converts_aware(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        noon_eastern = datetime(2026, 1, 15, 12, 0, tzinfo=eastern)
        converted = ensure_utc(noon_eastern)
        assert converted.utcoffset() == timedelta(0)
        assert converted.hour == 17

    def test_iso_round_trip(self) -> None:
        original = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
        assert from_iso(to_iso(original)) == original

    def test_day_bounds_are_half_open(self) -> None:
        """An inclusive 23:59:59 bound would drop the final second of events."""
        day = date(2026, 1, 15)
        assert start_of_day(day) == datetime(2026, 1, 15, tzinfo=UTC)
        assert end_of_day(day) == datetime(2026, 1, 16, tzinfo=UTC)
        assert end_of_day(day) - start_of_day(day) == timedelta(days=1)

    def test_start_of_day_accepts_a_datetime(self) -> None:
        moment = datetime(2026, 1, 15, 23, 59, 59, tzinfo=UTC)
        assert start_of_day(moment) == datetime(2026, 1, 15, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("start", "days", "expected"),
        [
            (date(2026, 1, 15), 1, date(2026, 1, 16)),  # Thu -> Fri
            (date(2026, 1, 16), 1, date(2026, 1, 19)),  # Fri -> Mon, skips weekend
            (date(2026, 1, 16), 3, date(2026, 1, 21)),
            (date(2026, 1, 19), -1, date(2026, 1, 16)),  # Mon -> Fri backwards
            (date(2026, 1, 17), 0, date(2026, 1, 17)),  # zero is a no-op
        ],
    )
    def test_add_business_days(self, start: date, days: int, expected: date) -> None:
        assert add_business_days(start, days) == expected

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(0), "0s"),
            (timedelta(seconds=45), "45s"),
            (timedelta(minutes=3, seconds=7), "3m 7s"),
            (timedelta(hours=3, minutes=12), "3h 12m"),
            (timedelta(days=2, hours=5), "2d 5h"),
            (timedelta(seconds=-90), "-1m 30s"),
        ],
    )
    def test_humanize_timedelta(self, delta: timedelta, expected: str) -> None:
        assert humanize_timedelta(delta) == expected


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Hello World", "hello-world"),
            ("  Leading and trailing  ", "leading-and-trailing"),
            ("Multiple   spaces", "multiple-spaces"),
            ("Special!@#chars", "special-chars"),
            ("Café", "cafe"),
            ("already-a-slug", "already-a-slug"),
            ("---dashes---", "dashes"),
            ("!!!", ""),
        ],
    )
    def test_slugify(self, raw: str, expected: str) -> None:
        assert strings.slugify(raw) == expected

    def test_transliteration_is_lossy_and_can_collide(self) -> None:
        """Documented behaviour: slugs are not unique, so callers must check."""
        assert strings.slugify("Café") == strings.slugify("Cafe")

    def test_truncates_without_trailing_dash(self) -> None:
        assert not strings.slugify("hello world foo", max_length=12).endswith("-")


class TestTruncate:
    def test_short_string_is_unchanged(self) -> None:
        assert strings.truncate("short", 20) == "short"

    def test_result_respects_the_budget(self) -> None:
        assert len(strings.truncate("a" * 100, 20)) <= 20

    def test_prefers_a_nearby_word_boundary(self) -> None:
        """A boundary in the last quarter of the budget is worth cutting back to."""
        assert strings.truncate("hello beautiful world", 20) == "hello beautiful…"

    def test_cuts_mid_word_when_the_boundary_is_too_far_back(self) -> None:
        """Otherwise most of the budget would be thrown away for tidiness."""
        assert strings.truncate("hello beautiful world", 14) == "hello beautif…"

    def test_rejects_a_budget_smaller_than_the_suffix(self) -> None:
        with pytest.raises(ValueError, match="max_length"):
            strings.truncate("hello", 0)


class TestMaskAndRedact:
    def test_mask_keeps_only_the_tail(self) -> None:
        masked = strings.mask("sk-live-abcdefghijkl")
        assert masked.endswith("ijkl")
        assert "abcdefgh" not in masked

    def test_short_values_are_fully_masked(self) -> None:
        """Revealing four of five characters is not redaction."""
        assert strings.mask("abcde") == "*****"

    def test_redacts_nested_and_listed_secrets(self) -> None:
        redacted = strings.redact_sensitive(
            {
                "email": "a@b.c",
                "password": "hunter2",
                "nested": {"api_key": "sk-live", "safe": 1},
                "items": [{"token": "t"}],
            }
        )
        assert redacted["email"] == "a@b.c"
        assert redacted["password"] == REDACTED
        assert redacted["nested"]["api_key"] == REDACTED
        assert redacted["nested"]["safe"] == 1
        assert redacted["items"][0]["token"] == REDACTED

    def test_matching_is_case_insensitive(self) -> None:
        assert (
            strings.redact_sensitive({"Authorization": "Bearer x"})["Authorization"]
            == REDACTED
        )

    def test_input_is_not_mutated(self) -> None:
        original = {"password": "hunter2"}
        strings.redact_sensitive(original)
        assert original["password"] == "hunter2"


class TestCaseConversion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("camelCase", "camel_case"),
            ("PascalCase", "pascal_case"),
            ("HTTPResponse", "http_response"),
            ("already_snake", "already_snake"),
        ],
    )
    def test_to_snake_case(self, raw: str, expected: str) -> None:
        assert strings.to_snake_case(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("snake_case", "snakeCase"), ("a_b_c", "aBC"), ("single", "single")],
    )
    def test_to_camel_case(self, raw: str, expected: str) -> None:
        assert strings.to_camel_case(raw) == expected

    def test_normalize_email_lowercases_and_strips(self) -> None:
        assert strings.normalize_email("  User@Example.COM ") == "user@example.com"

    def test_normalize_email_preserves_plus_tags(self) -> None:
        """Stripping +tags is an anti-abuse policy, not a formatting rule."""
        assert strings.normalize_email("a+tag@example.com") == "a+tag@example.com"


class TestCollections:
    def test_chunk_splits_evenly_and_keeps_the_remainder(self) -> None:
        chunks = [list(c) for c in cols.chunk([1, 2, 3, 4, 5], 2)]
        assert chunks == [[1, 2], [3, 4], [5]]

    def test_chunk_of_empty_yields_nothing(self) -> None:
        assert list(cols.chunk([], 3)) == []

    def test_chunk_rejects_non_positive_size(self) -> None:
        """A zero size would loop forever."""
        with pytest.raises(ValueError, match="positive"):
            list(cols.chunk([1, 2], 0))

    def test_group_by_preserves_encounter_order(self) -> None:
        grouped = cols.group_by(["apple", "avocado", "banana"], lambda s: s[0])
        assert grouped == {"a": ["apple", "avocado"], "b": ["banana"]}

    def test_index_by_keeps_the_last_on_collision(self) -> None:
        assert cols.index_by([(1, "a"), (1, "b")], lambda t: t[0]) == {1: (1, "b")}

    def test_unique_preserves_order(self) -> None:
        assert cols.unique([3, 1, 3, 2, 1]) == [3, 1, 2]

    def test_unique_with_key(self) -> None:
        assert cols.unique(["a", "A", "b"], key=str.lower) == ["a", "b"]

    def test_partition_evaluates_predicate_once_per_item(self) -> None:
        calls: list[int] = []

        def even(n: int) -> bool:
            calls.append(n)
            return n % 2 == 0

        assert cols.partition([1, 2, 3, 4], even) == ([2, 4], [1, 3])
        assert calls == [1, 2, 3, 4]

    def test_flatten_one_level(self) -> None:
        assert cols.flatten([[1, 2], [3], []]) == [1, 2, 3]

    def test_deep_merge_recurses_into_dicts(self) -> None:
        merged = cols.deep_merge(
            {"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"y": 3}, "c": 4}
        )
        assert merged == {"a": {"x": 1, "y": 3}, "b": 1, "c": 4}

    def test_deep_merge_replaces_lists_outright(self) -> None:
        """Merging lists has no single obvious meaning, so replacement wins."""
        assert cols.deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}

    def test_deep_merge_does_not_mutate_inputs(self) -> None:
        base = {"a": {"x": 1}}
        cols.deep_merge(base, {"a": {"y": 2}})
        assert base == {"a": {"x": 1}}

    def test_the_result_shares_no_nested_structure_with_the_base(self) -> None:
        """The bug this catches, which "does not mutate" above did not.

        A shallow ``dict(base)`` leaves every nested dict aliased. The merge
        itself then looks correct — the assertion above passes — and the damage
        happens later, when a caller mutates the result::

            config = deep_merge(DEFAULTS, overrides)
            config["db"]["host"] = "localhost"

        which rewrites ``DEFAULTS`` for the life of the process. Merging
        configuration is exactly where a shared default is the base.
        """
        base = {"db": {"host": "prod", "nested": {"deep": 1}}}
        merged = cols.deep_merge(base, {"flags": {"on": True}})

        merged["db"]["host"] = "localhost"
        merged["db"]["nested"]["deep"] = 99

        assert base == {"db": {"host": "prod", "nested": {"deep": 1}}}

    def test_the_result_shares_no_nested_structure_with_the_override(self) -> None:
        """The same hazard from the other side."""
        override = {"db": {"host": "local"}}
        merged = cols.deep_merge({}, override)

        merged["db"]["host"] = "mutated"

        assert override == {"db": {"host": "local"}}

    def test_lists_in_the_result_are_copies_too(self) -> None:
        """A replaced list is still a shared object unless it is copied."""
        base = {"items": [{"x": 1}]}
        merged = cols.deep_merge(base, {})

        merged["items"][0]["x"] = 99
        merged["items"].append("new")

        assert base == {"items": [{"x": 1}]}

    def test_merged_branches_are_also_independent(self) -> None:
        """Both sides contributed, so both must be safe from the result."""
        base = {"a": {"x": {"deep": 1}}}
        override = {"a": {"y": {"deep": 2}}}
        merged = cols.deep_merge(base, override)

        merged["a"]["x"]["deep"] = 99
        merged["a"]["y"]["deep"] = 99

        assert base == {"a": {"x": {"deep": 1}}}
        assert override == {"a": {"y": {"deep": 2}}}

    def test_compact_drops_none_but_keeps_falsy(self) -> None:
        assert cols.compact({"a": 1, "b": None, "c": 0, "d": ""}) == {
            "a": 1,
            "c": 0,
            "d": "",
        }


class TestCrypto:
    def test_tokens_are_unique_and_long(self) -> None:
        tokens = {crypto.generate_token() for _ in range(100)}
        assert len(tokens) == 100
        assert all(len(t) >= 40 for t in tokens)

    def test_numeric_code_length_and_digits(self) -> None:
        code = crypto.generate_numeric_code(6)
        assert len(code) == 6
        assert code.isdigit()

    def test_numeric_code_can_have_leading_zeros(self) -> None:
        """Zero-padding keeps the distribution uniform across the full range."""
        codes = {crypto.generate_numeric_code(2) for _ in range(500)}
        assert all(len(c) == 2 for c in codes)

    def test_numeric_code_rejects_non_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            crypto.generate_numeric_code(0)

    def test_constant_time_compare(self) -> None:
        assert crypto.constant_time_compare("secret", "secret") is True
        assert crypto.constant_time_compare("secret", "secreT") is False
        assert crypto.constant_time_compare(b"bytes", "bytes") is True

    def test_hash_token_is_deterministic_and_hides_input(self) -> None:
        hashed = crypto.hash_token("tok_abc")
        assert hashed == crypto.hash_token("tok_abc")
        assert "tok_abc" not in hashed
        assert len(hashed) == 64

    def test_signature_round_trip(self) -> None:
        payload = b'{"event":"invoice.paid"}'
        signature = crypto.sign_payload(payload, "shared-secret")
        assert crypto.verify_signature(payload, signature, "shared-secret") is True

    def test_signature_rejects_tampered_payload(self) -> None:
        signature = crypto.sign_payload(b"original", "shared-secret")
        assert crypto.verify_signature(b"tampered", signature, "shared-secret") is False

    def test_signature_rejects_wrong_secret(self) -> None:
        signature = crypto.sign_payload(b"payload", "right")
        assert crypto.verify_signature(b"payload", signature, "wrong") is False

    async def test_hash_stream_matches_hash_bytes(self) -> None:
        async def source():
            yield b"hello "
            yield b"world"

        assert await crypto.hash_stream(source()) == crypto.hash_bytes(b"hello world")


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("photo.png", "photo.png"),
            ("/absolute/path.txt", "path.txt"),
            ("windows\\path.txt", "path.txt"),
            ("", "unnamed"),
            ("...", "unnamed"),
        ],
    )
    def test_sanitize_filename(self, raw: str, expected: str) -> None:
        assert files.sanitize_filename(raw) == expected

    def test_traversal_components_are_removed(self) -> None:
        assert files.sanitize_filename("../../etc/passwd") == "passwd"

    def test_null_bytes_and_controls_are_stripped(self) -> None:
        assert "\x00" not in files.sanitize_filename("evil\x00.png")

    def test_rtl_override_is_stripped(self) -> None:
        """U+202E reverses display, making an .exe look like a .png."""
        rlo = "\u202e"
        assert rlo not in files.sanitize_filename(f"photo{rlo}gnp.exe")

    def test_get_extension(self) -> None:
        assert files.get_extension("Photo.PNG") == ".png"
        assert files.get_extension("no_extension") == ""


class TestContentSniffing:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (b"\x89PNG\r\n\x1a\n rest", "image/png"),
            (b"GIF89a...", "image/gif"),
            (b"%PDF-1.7", "application/pdf"),
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"PK\x03\x04", "application/zip"),
        ],
    )
    def test_detects_known_signatures(self, header: bytes, expected: str) -> None:
        assert files.detect_content_type(header) == expected

    def test_detects_svg_which_has_no_magic_number(self) -> None:
        """SVG can carry <script>, so it must never be mistaken for a raw image."""
        assert files.detect_content_type(b'<svg xmlns="...">') == "image/svg+xml"

    def test_unknown_content_returns_none(self) -> None:
        """None means 'unknown', so the caller must reject rather than assume."""
        assert files.detect_content_type(b"just some text") is None

    def test_extension_lies_are_caught_by_sniffing(self) -> None:
        """An HTML payload renamed to .png is the classic stored-XSS route."""
        assert files.detect_content_type(b"<html><script>") != "image/png"

    def test_allow_list_is_case_insensitive(self) -> None:
        allowed = frozenset({"image/png"})
        assert files.is_allowed_content_type("IMAGE/PNG", allowed) is True
        assert files.is_allowed_content_type("image/svg+xml", allowed) is False


class TestStreaming:
    async def test_rechunks_to_the_requested_size(self) -> None:
        async def source():
            yield b"abcde"
            yield b"fghij"

        chunks = [c async for c in files.stream_chunks(source(), chunk_size=3)]
        assert b"".join(chunks) == b"abcdefghij"
        assert all(len(c) <= 3 for c in chunks)

    async def test_enforces_the_ceiling_mid_stream(self) -> None:
        """Content-Length cannot be trusted, so the limit is enforced as it reads."""

        async def source():
            for _ in range(10):
                yield b"x" * 100

        with pytest.raises(ValueError, match="exceeded"):
            async for _ in files.stream_chunks(source(), max_bytes=250):
                pass

    @pytest.mark.parametrize(
        ("size", "expected"),
        [(512, "512 B"), (2_400_000, "2.4 MB"), (1_500, "1.5 kB")],
    )
    def test_human_readable_size(self, size: int, expected: str) -> None:
        assert files.human_readable_size(size) == expected


class TestSafeJoin:
    def test_resolves_beneath_the_root(self, tmp_path) -> None:
        assert files.safe_join(tmp_path, "a/b.txt").is_relative_to(tmp_path.resolve())

    def test_refuses_to_escape_the_root(self, tmp_path) -> None:
        """Without this check, an upload key can write anywhere."""
        with pytest.raises(ValueError, match="escapes"):
            files.safe_join(tmp_path, "../../etc/passwd")
