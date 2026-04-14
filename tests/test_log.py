"""Tests for the logging module."""

import io
import json
import logging

from localforge.log import setup_logging, JSONFormatter


def test_human_format():
    buf = io.StringIO()
    setup_logging(fmt="human", stream=buf)
    log = logging.getLogger("localforge")
    log.info("test message")
    output = buf.getvalue()
    assert "test message" in output
    assert "[INFO]" in output


def test_json_format():
    buf = io.StringIO()
    setup_logging(fmt="json", stream=buf)
    log = logging.getLogger("localforge")
    log.info("json test")
    output = buf.getvalue().strip()
    data = json.loads(output)
    assert data["msg"] == "json test"
    assert data["level"] == "INFO"
    assert data["logger"] == "localforge"


def test_json_extra_fields():
    buf = io.StringIO()
    setup_logging(fmt="json", stream=buf)
    log = logging.getLogger("localforge")
    log.info("tool call", extra={"tool": "local_chat", "duration_ms": 500})
    data = json.loads(buf.getvalue().strip())
    assert data["tool"] == "local_chat"
    assert data["duration_ms"] == 500


def test_log_level_filtering():
    buf = io.StringIO()
    setup_logging(fmt="human", level="WARNING", stream=buf)
    log = logging.getLogger("localforge")
    log.info("should not appear")
    log.warning("should appear")
    output = buf.getvalue()
    assert "should not appear" not in output
    assert "should appear" in output


def test_sub_logger():
    buf = io.StringIO()
    setup_logging(fmt="json", stream=buf)
    log = logging.getLogger("localforge.client")
    log.info("sub logger test")
    data = json.loads(buf.getvalue().strip())
    assert data["logger"] == "localforge.client"
