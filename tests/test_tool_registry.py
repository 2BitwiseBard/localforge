"""Tests for the tool registration system."""

from localforge.tools import (  # noqa: F401
    _tool_definitions,
    _tool_handlers,
    agents_tools,
    chat,
    config_tools,
    context,
    filesystem,
    git,
    infrastructure,
    memory,
    sessions,
    shell,
    web,
)


def test_definitions_match_handlers():
    """Every tool definition has a matching handler — counts must agree."""
    assert len(_tool_definitions) == len(_tool_handlers)


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
    """Spot-check that the core skeleton tools are registered."""
    names = {t.name for t in _tool_definitions}
    expected = {
        # Chat
        "local_chat",
        "multi_turn_chat",
        # Infrastructure
        "health_check",
        "swap_model",
        "benchmark",
        "sync_models",
        # Filesystem + shell
        "fs_read",
        "fs_list",
        "fs_glob",
        "fs_grep",
        "fs_write",
        "fs_edit",
        "fs_delete",
        "shell_exec",
        # Web
        "web_search",
        "web_fetch",
        # Git + context
        "git_context",
        "set_context",
        "auto_context",
        # Agents
        "agent_list",
    }
    missing = expected - names
    assert not missing, f"Missing tools: {missing}"
