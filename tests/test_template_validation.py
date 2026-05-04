"""Ensure every YAML workflow template passes strict schema validation.

Any .yaml/.yml file in the repository whose top-level dict contains a
``nodes`` key is treated as a workflow definition.  The test loads it via
WorkflowDef.from_yaml() and asserts that WorkflowDef.validate() returns no
errors.  This catches silent config-key drift between schema.py and the
template files whenever models are updated.
"""

from pathlib import Path

import pytest
import yaml

from localforge.workflows.schema import WorkflowDef

_REPO_ROOT = Path(__file__).parent.parent


def _workflow_yaml_files() -> list[Path]:
    results: list[Path] = []
    for path in sorted(_REPO_ROOT.rglob("*.yaml")) + sorted(_REPO_ROOT.rglob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            continue
        if isinstance(data, dict) and "nodes" in data:
            results.append(path)
    return results


_WORKFLOW_YAMLS = _workflow_yaml_files()


@pytest.mark.parametrize("yaml_path", _WORKFLOW_YAMLS, ids=lambda p: p.name)
def test_workflow_template_loads(yaml_path: Path):
    """WorkflowDef.from_yaml() must not raise for any workflow YAML."""
    WorkflowDef.from_yaml(yaml_path)  # raises on malformed YAML or missing required keys


@pytest.mark.parametrize("yaml_path", _WORKFLOW_YAMLS, ids=lambda p: p.name)
def test_workflow_template_validates(yaml_path: Path):
    """WorkflowDef.validate() must return no errors for any workflow YAML."""
    wf = WorkflowDef.from_yaml(yaml_path)
    errors = wf.validate()
    assert not errors, (
        f"{yaml_path.name} has {len(errors)} validation error(s):\n"
        + "\n".join(f"  • {e}" for e in errors)
    )


class TestSchemaValidationLogic:
    """Unit tests for the enhanced validate() config-checking rules."""

    def _make_wf(self, nodes: list[dict], edges: list[dict] = None, variables: dict = None) -> WorkflowDef:
        return WorkflowDef.from_dict({
            "id": "test-wf",
            "name": "Test",
            "nodes": nodes,
            "edges": edges or [],
            "variables": variables or {},
        })

    # prompt nodes -----------------------------------------------------------

    def test_prompt_missing_template(self):
        wf = self._make_wf([{"id": "n", "type": "prompt", "config": {}}])
        errors = wf.validate()
        assert any("missing 'template'" in e for e in errors)

    def test_prompt_valid(self):
        wf = self._make_wf([{"id": "n", "type": "prompt", "config": {"template": "Hello"}}])
        assert wf.validate() == []

    def test_prompt_unknown_node_ref(self):
        wf = self._make_wf([
            {"id": "n", "type": "prompt", "config": {"template": "Result: {node.missing}"}}
        ])
        errors = wf.validate()
        assert any("unknown node 'missing'" in e for e in errors)

    def test_prompt_unknown_variable_ref(self):
        wf = self._make_wf([
            {"id": "n", "type": "prompt", "config": {"template": "{variables.ghost}"}}
        ])
        errors = wf.validate()
        assert any("undeclared variable 'ghost'" in e for e in errors)

    def test_prompt_declared_variable_ref_ok(self):
        wf = self._make_wf(
            [{"id": "n", "type": "prompt", "config": {"template": "{variables.topic}"}}],
            variables={"topic": "python"},
        )
        assert wf.validate() == []

    # tool nodes -------------------------------------------------------------

    def test_tool_missing_tool_name(self):
        wf = self._make_wf([{"id": "n", "type": "tool", "config": {"arguments": {}}}])
        errors = wf.validate()
        assert any("missing 'tool_name'" in e for e in errors)

    def test_tool_valid(self):
        wf = self._make_wf([{"id": "n", "type": "tool", "config": {"tool_name": "git_context", "arguments": {}}}])
        assert wf.validate() == []

    def test_tool_arguments_not_dict(self):
        wf = self._make_wf([{"id": "n", "type": "tool", "config": {"tool_name": "x", "arguments": "bad"}}])
        errors = wf.validate()
        assert any("'arguments' must be a mapping" in e for e in errors)

    def test_tool_argument_unknown_node_ref(self):
        wf = self._make_wf([{
            "id": "n", "type": "tool",
            "config": {"tool_name": "x", "arguments": {"path": "{node.ghost}"}},
        }])
        errors = wf.validate()
        assert any("unknown node 'ghost'" in e for e in errors)

    # parallel nodes ---------------------------------------------------------

    def test_parallel_missing_node_ids(self):
        wf = self._make_wf([{"id": "n", "type": "parallel", "config": {}}])
        errors = wf.validate()
        assert any("'node_ids' must be a non-empty list" in e for e in errors)

    def test_parallel_unknown_child(self):
        wf = self._make_wf([{"id": "n", "type": "parallel", "config": {"node_ids": ["ghost"]}}])
        errors = wf.validate()
        assert any("unknown node 'ghost'" in e for e in errors)

    def test_parallel_valid(self):
        wf = self._make_wf([
            {"id": "a", "type": "prompt", "config": {"template": "A"}},
            {"id": "b", "type": "prompt", "config": {"template": "B"}},
            {"id": "p", "type": "parallel", "config": {"node_ids": ["a", "b"]}},
        ])
        assert wf.validate() == []

    # condition nodes --------------------------------------------------------

    def test_condition_unknown_true_node(self):
        wf = self._make_wf([{"id": "n", "type": "condition", "config": {"expression": "True", "true_node": "ghost"}}])
        errors = wf.validate()
        assert any("'true_node' references unknown node 'ghost'" in e for e in errors)

    def test_condition_valid_no_targets(self):
        wf = self._make_wf([{"id": "n", "type": "condition", "config": {"expression": "True"}}])
        assert wf.validate() == []

    # loop nodes -------------------------------------------------------------

    def test_loop_missing_node_ids(self):
        wf = self._make_wf([{"id": "n", "type": "loop", "config": {}}])
        errors = wf.validate()
        assert any("'node_ids' must be a non-empty list" in e for e in errors)

    def test_loop_unknown_child(self):
        wf = self._make_wf([{"id": "n", "type": "loop", "config": {"node_ids": ["ghost"]}}])
        errors = wf.validate()
        assert any("unknown node 'ghost'" in e for e in errors)

    # set_variable nodes -----------------------------------------------------

    def test_set_variable_missing_name(self):
        wf = self._make_wf([{"id": "n", "type": "set_variable", "config": {"value_template": "x"}}])
        errors = wf.validate()
        assert any("missing 'name'" in e for e in errors)

    def test_set_variable_missing_value_template(self):
        wf = self._make_wf([{"id": "n", "type": "set_variable", "config": {"name": "x"}}])
        errors = wf.validate()
        assert any("missing 'value_template'" in e for e in errors)

    def test_set_variable_valid(self):
        wf = self._make_wf([{"id": "n", "type": "set_variable", "config": {"name": "result", "value_template": "done"}}])
        assert wf.validate() == []

    # edge and graph checks --------------------------------------------------

    def test_edge_unknown_from(self):
        wf = self._make_wf(
            [{"id": "a", "type": "prompt", "config": {"template": "A"}}],
            edges=[{"from_id": "ghost", "to_id": "a"}],
        )
        errors = wf.validate()
        assert any("Edge from unknown node 'ghost'" in e for e in errors)

    def test_no_root_nodes(self):
        wf = self._make_wf(
            [
                {"id": "a", "type": "prompt", "config": {"template": "A"}},
                {"id": "b", "type": "prompt", "config": {"template": "B"}},
            ],
            edges=[{"from_id": "a", "to_id": "b"}, {"from_id": "b", "to_id": "a"}],
        )
        errors = wf.validate()
        assert any("No root nodes" in e for e in errors)

    def test_unknown_node_type(self):
        wf = self._make_wf([{"id": "n", "type": "teleport", "config": {}}])
        errors = wf.validate()
        assert any("unknown type 'teleport'" in e for e in errors)


class TestYamlSchemaValidatorDiscovery:
    """Tests for the scanner helpers used by yaml_schema_validator and CI."""

    def test_discover_finds_bundled_templates(self):
        """discover_workflow_yamls on the repo root finds all four bundled templates."""
        from localforge.workflows.scanner import discover_workflow_yamls

        found = discover_workflow_yamls(_REPO_ROOT)
        names = {p.name for p in found}
        assert {"deep-analyze.yaml", "full-review.yaml",
                "onboard-project.yaml", "validate-templates.yaml"}.issubset(names)

    def test_discover_ignores_non_workflow_yaml(self, tmp_path):
        """Files without a top-level 'nodes' key are skipped."""
        from localforge.workflows.scanner import discover_workflow_yamls

        (tmp_path / "config.yaml").write_text("key: value\n")
        (tmp_path / "workflow.yaml").write_text(
            "id: w\nname: w\nnodes:\n"
            "  - id: a\n    type: prompt\n    config:\n      template: hi\n"
        )
        found = discover_workflow_yamls(tmp_path)
        assert len(found) == 1
        assert found[0].name == "workflow.yaml"

    def test_discover_skips_unparseable_yaml(self, tmp_path):
        """Malformed YAML files do not raise — they are silently skipped."""
        from localforge.workflows.scanner import discover_workflow_yamls

        (tmp_path / "broken.yaml").write_text("nodes: [\n  unclosed")
        found = discover_workflow_yamls(tmp_path)
        assert found == []

    def test_resolve_repo_root_auto_returns_repo(self):
        """resolve_repo_root('auto') returns the directory containing src/localforge."""
        from localforge.workflows.scanner import resolve_repo_root

        root = resolve_repo_root("auto")
        assert root.is_dir()
        assert (root / "src" / "localforge").is_dir()

    def test_resolve_repo_root_literal_path(self, tmp_path):
        """resolve_repo_root accepts a literal path string."""
        from localforge.workflows.scanner import resolve_repo_root

        result = resolve_repo_root(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_discover_repo_root_all_templates_valid(self):
        """Every template found via repo-root scan passes schema validation."""
        from localforge.workflows.scanner import discover_workflow_yamls
        from localforge.workflows.schema import WorkflowDef

        for path in discover_workflow_yamls(_REPO_ROOT):
            wf = WorkflowDef.from_yaml(path)
            errors = wf.validate()
            assert not errors, (
                f"{path.name} failed when discovered via repo root: "
                + ", ".join(errors)
            )
