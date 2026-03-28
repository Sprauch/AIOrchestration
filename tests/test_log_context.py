"""Tests for correlation-ID logging context."""

import logging

from agents.core.log_context import (
    CorrelationFilter,
    clear_correlation,
    get_agent_ctx,
    get_thread_ctx,
    set_correlation,
)


class TestCorrelationContextVars:
    """Verify set/get/clear of correlation context variables."""

    def test_defaults_are_dash(self):
        clear_correlation()
        assert get_thread_ctx() == "-"
        assert get_agent_ctx() == "-"

    def test_set_and_get(self):
        set_correlation("abcdef1234567890", "developer-1")
        assert get_thread_ctx() == "abcdef12"
        assert get_agent_ctx() == "developer-1"
        clear_correlation()

    def test_clear_resets_to_defaults(self):
        set_correlation("abcdef1234567890", "pm-1")
        clear_correlation()
        assert get_thread_ctx() == "-"
        assert get_agent_ctx() == "-"

    def test_short_thread_id_not_truncated(self):
        set_correlation("abc", "reviewer-1")
        assert get_thread_ctx() == "abc"
        clear_correlation()

    def test_empty_thread_id_defaults_to_dash(self):
        set_correlation("", "pm-1")
        assert get_thread_ctx() == "-"
        clear_correlation()


class TestCorrelationFilter:
    """Verify the logging filter injects context into log records."""

    def test_filter_adds_fields_with_context(self):
        set_correlation("deadbeef12345678", "architect-1")
        try:
            filt = CorrelationFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="hello", args=(), exc_info=None,
            )
            result = filt.filter(record)
            assert result is True
            assert record.thread_ctx == "deadbeef"  # type: ignore[attr-defined]
            assert record.agent_ctx == "architect-1"  # type: ignore[attr-defined]
        finally:
            clear_correlation()

    def test_filter_adds_defaults_without_context(self):
        clear_correlation()
        filt = CorrelationFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        filt.filter(record)
        assert record.thread_ctx == "-"  # type: ignore[attr-defined]
        assert record.agent_ctx == "-"  # type: ignore[attr-defined]

    def test_formatter_renders_fields(self):
        """End-to-end: a handler with the filter and formatter produces expected output."""
        set_correlation("cafebabe12345678", "developer-2")
        try:
            test_logger = logging.getLogger("test_correlation_e2e")
            test_logger.setLevel(logging.DEBUG)
            test_logger.handlers.clear()
            test_logger.propagate = False

            handler = logging.StreamHandler()
            handler.setLevel(logging.DEBUG)
            filt = CorrelationFilter()
            handler.addFilter(filt)
            handler.setFormatter(logging.Formatter(
                "%(levelname)s [%(thread_ctx)s %(agent_ctx)s]: %(message)s"
            ))
            test_logger.addHandler(handler)

            # Capture output
            import io
            stream = io.StringIO()
            handler.stream = stream

            test_logger.info("processing envelope")
            output = stream.getvalue()

            assert "cafebabe" in output
            assert "developer-2" in output
            assert "processing envelope" in output
        finally:
            clear_correlation()
