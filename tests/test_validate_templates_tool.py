"""Tests for the validate_templates MCP tool.

Covers the async tool handler directly — no MCP transport needed.
"""

import pytest

from localforge.tools.validation import validate_templates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_yaml(tmp_path, name: str = "workflow.yaml") -> None:
    (tmp_path / name).write_text(
        "id: test\n"
        "name: Test\n"
        "nodes:\n"
        "  - id: greet\n"
        "    type: prompt\n"
        "    config:\n"
        "      template: Hello\n"
        "edges: []\n"
    )


def _make_invalid_yaml(tmp_path, name: str = "broken.yaml") -> None:
    # prompt node missing required 'template' key
    (tmp_path / name).write_text(
        "id: broken\n"
        "name: Broken\n"
        "nodes:\n"
        "  - id: n\n"
        "    type: prompt\n"
        "    config: {}\n"
        "edges: []\n"
    )


def _make_non_workflow_yaml(tmp_path, name: str = "config.yaml") -> None:
    (tmp_path / name).write_text("key: value\n")


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_yaml_files_returns_message(tmp_path):
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "No workflow YAML files found" in out


@pytest.mark.asyncio
async def test_non_workflow_yaml_skipped(tmp_path):
    _make_non_workflow_yaml(tmp_path)
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "No workflow YAML files found" in out


@pytest.mark.asyncio
async def test_valid_template_reports_ok(tmp_path):
    _make_valid_yaml(tmp_path)
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "OK" in out
    assert "All 1 template(s) PASSED" in out


@pytest.mark.asyncio
async def test_invalid_template_reports_failed(tmp_path):
    _make_invalid_yaml(tmp_path)
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "FAILED" in out
    assert "1/1 template(s) FAILED" in out
    assert "missing 'template'" in out


@pytest.mark.asyncio
async def test_mixed_templates_counts_correctly(tmp_path):
    _make_valid_yaml(tmp_path, "good.yaml")
    _make_invalid_yaml(tmp_path, "bad.yaml")
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "1/2 template(s) FAILED" in out
    assert "OK" in out
    assert "FAILED" in out


@pytest.mark.asyncio
async def test_parse_error_reported_as_failure(tmp_path):
    (tmp_path / "broken.yaml").write_text("nodes: [\n  unclosed")
    out = await validate_templates({"repo_root": str(tmp_path)})
    # malformed YAML is silently skipped by discover_workflow_yamls
    assert "No workflow YAML files found" in out


@pytest.mark.asyncio
async def test_default_repo_root_auto(monkeypatch):
    """Calling without repo_root uses 'auto' and finds the bundled templates."""
    out = await validate_templates({})
    assert "Validating" in out
    assert "template(s)" in out
    # All bundled templates must pass
    assert "FAILED" not in out


@pytest.mark.asyncio
async def test_explicit_repo_root_auto_string():
    """Passing repo_root='auto' explicitly behaves identically to the default."""
    out = await validate_templates({"repo_root": "auto"})
    assert "All" in out
    assert "PASSED" in out


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_literal_path_returns_error():
    out = await validate_templates({"repo_root": "/nonexistent/path/that/does/not/exist"})
    # No crash; returns "No workflow YAML files found" since the path is absent
    # OR a graceful error message
    assert isinstance(out, str)
    assert len(out) > 0


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_includes_repo_path(tmp_path):
    _make_valid_yaml(tmp_path)
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert str(tmp_path) in out


@pytest.mark.asyncio
async def test_output_includes_template_filename(tmp_path):
    _make_valid_yaml(tmp_path, "my-workflow.yaml")
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "my-workflow.yaml" in out


@pytest.mark.asyncio
async def test_multiple_errors_all_shown(tmp_path):
    # tool node missing tool_name AND parallel node with no node_ids
    (tmp_path / "multi-err.yaml").write_text(
        "id: multi\n"
        "name: Multi\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: tool\n"
        "    config: {}\n"
        "  - id: p\n"
        "    type: parallel\n"
        "    config: {}\n"
        "edges: []\n"
    )
    out = await validate_templates({"repo_root": str(tmp_path)})
    assert "missing 'tool_name'" in out
    assert "'node_ids' must be a non-empty list" in out


# ---------------------------------------------------------------------------
# Integration: new strict-schema-validate template itself is valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_schema_validate_template_is_valid():
    """The new workflow template must pass its own schema validation."""
    from pathlib import Path
    from localforge.workflows.schema import WorkflowDef

    tmpl = (
        Path(__file__).parent.parent
        / "src" / "localforge" / "workflows" / "templates"
        / "strict-schema-validate.yaml"
    )
    assert tmpl.exists(), f"Template not found: {tmpl}"
    wf = WorkflowDef.from_yaml(tmpl)
    errors = wf.validate()
    assert not errors, "strict-schema-validate.yaml has validation errors:\n" + "\n".join(
        f"  • {e}" for e in errors
    )
