"""Shared test fixtures for LocalForge."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure src/ is on the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class MockHTTPResponse:
    """Mock httpx.Response for testing tool handlers without a running backend."""

    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self._text = text

    def json(self):
        return self._json

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=self,
            )


def make_chat_response(content: str = "mock response") -> MockHTTPResponse:
    """Create a mock chat completion response."""
    return MockHTTPResponse(json_data={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    })


def make_model_info_response(model_name: str = "test-model") -> MockHTTPResponse:
    """Create a mock model info response."""
    return MockHTTPResponse(json_data={
        "model_name": model_name,
        "lora_names": [],
    })


@pytest.fixture
def mock_httpx_client(monkeypatch):
    """Mock httpx.AsyncClient for testing tool handlers without a backend.

    Usage:
        def test_my_tool(mock_httpx_client):
            mock_httpx_client.set_chat_response("expected output")
            # ... call your tool handler ...

    The mock intercepts all POST/GET calls to the client pool and returns
    configurable responses.
    """
    _chat_content = "mock response"
    _model_name = "test-model"
    _custom_responses = {}  # url_suffix -> MockHTTPResponse

    class _Controller:
        def set_chat_response(self, content: str):
            nonlocal _chat_content
            _chat_content = content

        def set_model_name(self, name: str):
            nonlocal _model_name
            _model_name = name

        def set_response(self, url_suffix: str, response: MockHTTPResponse):
            """Set a custom response for a specific URL suffix."""
            _custom_responses[url_suffix] = response

    controller = _Controller()

    async def _mock_post(url, **kwargs):
        for suffix, resp in _custom_responses.items():
            if url.endswith(suffix):
                return resp
        if "chat/completions" in url:
            return make_chat_response(_chat_content)
        if "completions" in url:
            return MockHTTPResponse(json_data={
                "choices": [{"text": _chat_content}],
            })
        return MockHTTPResponse(json_data={"status": "ok"})

    async def _mock_get(url, **kwargs):
        for suffix, resp in _custom_responses.items():
            if url.endswith(suffix):
                return resp
        if "model/info" in url:
            return make_model_info_response(_model_name)
        if "health" in url:
            return MockHTTPResponse(json_data={"status": "ok"})
        return MockHTTPResponse(json_data={})

    # Patch the shared httpx client in client.py
    from localforge import client
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=_mock_post)
    mock_client.get = AsyncMock(side_effect=_mock_get)
    monkeypatch.setattr(client, "_client", mock_client)

    # Set a default model so tools don't try to resolve
    from localforge import config as cfg
    monkeypatch.setattr(cfg, "MODEL", _model_name)

    return controller


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Set LOCALFORGE_DATA_DIR to a temp directory for isolated tests.

    Ensures databases, indexes, notes, etc. don't pollute the real data dir.
    """
    monkeypatch.setenv("LOCALFORGE_DATA_DIR", str(tmp_path))
    # Reset the cached data_dir so paths.py picks up the new env var
    from localforge import paths
    paths._DATA_DIR = None
    yield tmp_path
    paths._DATA_DIR = None
