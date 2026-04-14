"""Tests for auto_context project detection."""

import os
import tempfile
from pathlib import Path

from localforge.tools.context import _detect_project


class TestDetectProject:
    def test_rust_project(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "my-crate"\nversion = "0.1.0"\n')
        result = _detect_project(str(tmp_path))
        assert result["language"] == "rust"
        assert result["project_name"] == "my-crate"

    def test_python_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-pkg"\n')
        result = _detect_project(str(tmp_path))
        assert result["language"] == "python"
        assert result["project_name"] == "my-pkg"

    def test_python_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\nrequests\n")
        result = _detect_project(str(tmp_path))
        assert result["language"] == "python"
        # Falls back to dir name since requirements.txt has no project name
        assert result["project_name"] == tmp_path.name

    def test_node_project(self, tmp_path):
        import json
        (tmp_path / "package.json").write_text(json.dumps({"name": "my-app", "version": "1.0.0"}))
        result = _detect_project(str(tmp_path))
        assert result["language"] == "typescript"
        assert result["project_name"] == "my-app"

    def test_go_project(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/mygoapp\n\ngo 1.21\n")
        result = _detect_project(str(tmp_path))
        assert result["language"] == "go"
        assert result["project_name"] == "mygoapp"

    def test_unknown_project(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        result = _detect_project(str(tmp_path))
        assert "language" not in result

    def test_nonexistent_directory(self):
        result = _detect_project("/nonexistent/path/12345")
        assert result == {}

    def test_context_file(self, tmp_path):
        import yaml
        ctx = {"language": "zig", "project": "myzig", "rules": "no allocations"}
        (tmp_path / ".localforge-context.yaml").write_text(yaml.dump(ctx))
        result = _detect_project(str(tmp_path))
        assert result["language"] == "zig"
        assert result["project_name"] == "myzig"
        assert result["rules"] == "no allocations"

    def test_manifest_priority_over_fallback(self, tmp_path):
        """First matching manifest wins."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "rs"\n')
        (tmp_path / "package.json").write_text('{"name": "js"}')
        result = _detect_project(str(tmp_path))
        assert result["language"] == "rust"  # Cargo.toml is checked first

    def test_context_file_overrides_manifest(self, tmp_path):
        """Context file language takes precedence."""
        import yaml
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "rs"\n')
        (tmp_path / ".localforge-context.yaml").write_text(yaml.dump({"language": "python"}))
        result = _detect_project(str(tmp_path))
        assert result["language"] == "python"  # Context file overrides
