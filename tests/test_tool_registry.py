"""Tests for the tool registration system."""

from localforge.tools import (  # noqa: F401
    _tool_definitions,
    _tool_handlers,
    agents_tools,
    analysis,
    chat,
    compute,
    config_tools,
    context,
    diff,
    generation,
    git,
    infrastructure,
    knowledge,
    memory,
    orchestration,
    parallel,
    presets,
    rag,
    semantic,
    sessions,
    training,
    web,
)

EXPECTED_TOOL_COUNT = 112  # 111 + kg_rebuild_fts


def test_all_tools_registered():
    """All 101 tools must be registered."""
    assert len(_tool_definitions) == EXPECTED_TOOL_COUNT


def test_all_handlers_registered():
    """Every tool definition has a matching handler."""
    assert len(_tool_handlers) == EXPECTED_TOOL_COUNT


def test_no_duplicate_names():
    """No two tools share the same name."""
    names = [t.name for t in _tool_definitions]
    assert len(names) == len(set(names)), f"Duplicates: {[n for n in names if names.count(n) > 1]}"


def test_every_definition_has_handler():
    """Every tool_definition name has a corresponding handler in the registry."""
    for tool_def in _tool_definitions:
        assert tool_def.name in _tool_handlers, f"Missing handler: {tool_def.name}"


def test_handlers_are_async():
    """All handlers must be async (coroutine functions)."""
    import asyncio
    for name, handler in _tool_handlers.items():
        assert asyncio.iscoroutinefunction(handler), f"{name} is not async"


def test_tool_schemas_have_type():
    """Every tool schema should have 'type': 'object'."""
    for tool_def in _tool_definitions:
        schema = tool_def.inputSchema
        assert schema.get("type") == "object", f"{tool_def.name} schema missing type=object"


def test_known_tools_present():
    """Spot-check that key tools are registered."""
    names = {t.name for t in _tool_definitions}
    expected = {
        "local_chat", "health_check", "swap_model", "rag_query",
        "index_directory", "set_context", "review_diff", "fan_out",
        "kg_query", "web_search", "compute_status", "agent_list",
        "hybrid_search", "workflow", "benchmark", "mesh_dispatch",
        "auto_context", "train_start", "train_status", "train_prepare",
        "train_list", "train_feedback", "sync_models",
        "compute_test", "kg_rebuild_fts",
    }
    missing = expected - names
    assert not missing, f"Missing tools: {missing}"
