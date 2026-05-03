"""Tests for the filesystem MCP tools."""

import pytest

from localforge import config as cfg
from localforge.tools import filesystem


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Configure tmp_path as the only workspace root and yield it."""
    monkeypatch.setitem(cfg._config, "tool_workspaces", [str(tmp_path)])
    return tmp_path


# ---------------------------------------------------------------------------
# fs_read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_read_returns_numbered_lines(workspace):
    p = workspace / "hello.txt"
    p.write_text("alpha\nbeta\ngamma\n")
    out = await filesystem.fs_read({"path": str(p)})
    assert "1\talpha" in out
    assert "2\tbeta" in out
    assert "3\tgamma" in out


@pytest.mark.asyncio
async def test_fs_read_offset_and_limit(workspace):
    p = workspace / "many.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n")
    out = await filesystem.fs_read({"path": str(p), "offset": 5, "limit": 3})
    assert "5\tline5" in out
    assert "6\tline6" in out
    assert "7\tline7" in out
    assert "line4" not in out
    assert "line8" not in out


@pytest.mark.asyncio
async def test_fs_read_byte_cap(workspace, monkeypatch):
    monkeypatch.setattr(filesystem, "MAX_READ_BYTES", 64)
    p = workspace / "big.txt"
    p.write_text("x" * 200)
    out = await filesystem.fs_read({"path": str(p)})
    assert "Error: file too large" in out


@pytest.mark.asyncio
async def test_fs_read_outside_workspace_rejected(workspace):
    out = await filesystem.fs_read({"path": "/etc/passwd"})
    assert "Error: path outside workspace" in out


@pytest.mark.asyncio
async def test_fs_read_traversal_rejected(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    target = outside / "secret.txt"
    target.write_text("secret")
    # Path that *appears* to live in workspace but resolves outside via ..
    sneaky = workspace / ".." / outside.name / "secret.txt"
    out = await filesystem.fs_read({"path": str(sneaky)})
    assert "Error: path outside workspace" in out


@pytest.mark.asyncio
async def test_fs_read_symlink_escape_rejected(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside_target")
    target = outside / "secret.txt"
    target.write_text("secret content")
    link = workspace / "innocuous-link"
    link.symlink_to(target)
    out = await filesystem.fs_read({"path": str(link)})
    assert "Error: path outside workspace" in out


# ---------------------------------------------------------------------------
# fs_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_list_returns_entries(workspace):
    (workspace / "a.txt").write_text("a")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "nested.txt").write_text("nested")
    out = await filesystem.fs_list({"path": str(workspace)})
    assert "a.txt" in out
    assert "sub" in out
    assert "nested.txt" not in out  # non-recursive


@pytest.mark.asyncio
async def test_fs_list_outside_workspace_rejected(workspace):
    out = await filesystem.fs_list({"path": "/etc"})
    assert "Error: path outside workspace" in out


# ---------------------------------------------------------------------------
# fs_glob
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_glob_recursive(workspace):
    (workspace / "a.py").write_text("")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.py").write_text("")
    (workspace / "sub" / "c.txt").write_text("")
    out = await filesystem.fs_glob({"root": str(workspace), "pattern": "**/*.py"})
    assert "a.py" in out
    assert "b.py" in out
    assert "c.txt" not in out


# ---------------------------------------------------------------------------
# fs_grep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_grep_finds_matches(workspace):
    (workspace / "f1.txt").write_text("foo bar\nbaz qux\n")
    (workspace / "f2.txt").write_text("nothing here\nfoo again\n")
    out = await filesystem.fs_grep({"pattern": "foo", "path": str(workspace)})
    assert "f1.txt" in out
    assert "f2.txt" in out


@pytest.mark.asyncio
async def test_fs_grep_no_matches(workspace):
    (workspace / "f.txt").write_text("nothing relevant\n")
    out = await filesystem.fs_grep({"pattern": "needle", "path": str(workspace)})
    assert "no matches" in out


# ---------------------------------------------------------------------------
# fs_write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_write_creates_file(workspace):
    p = workspace / "new.txt"
    out = await filesystem.fs_write({"path": str(p), "content": "hello"})
    assert "Wrote" in out
    assert p.read_text() == "hello"


@pytest.mark.asyncio
async def test_fs_write_overwrites(workspace):
    p = workspace / "existing.txt"
    p.write_text("old")
    await filesystem.fs_write({"path": str(p), "content": "new"})
    assert p.read_text() == "new"


@pytest.mark.asyncio
async def test_fs_write_outside_workspace_rejected(workspace):
    out = await filesystem.fs_write({"path": "/tmp/escape.txt", "content": "x"})
    assert "Error: path outside workspace" in out


@pytest.mark.asyncio
async def test_fs_write_missing_parent_rejected(workspace):
    p = workspace / "no" / "such" / "dir" / "f.txt"
    out = await filesystem.fs_write({"path": str(p), "content": "x"})
    assert "parent directory does not exist" in out


# ---------------------------------------------------------------------------
# fs_edit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_edit_unique_replacement(workspace):
    p = workspace / "f.py"
    p.write_text("def foo(): return 1\n")
    out = await filesystem.fs_edit({
        "path": str(p),
        "old_string": "return 1",
        "new_string": "return 42",
    })
    assert "Edited" in out
    assert p.read_text() == "def foo(): return 42\n"


@pytest.mark.asyncio
async def test_fs_edit_missing_old_string(workspace):
    p = workspace / "f.txt"
    p.write_text("hello world\n")
    out = await filesystem.fs_edit({
        "path": str(p),
        "old_string": "missing",
        "new_string": "x",
    })
    assert "old_string not found" in out
    assert p.read_text() == "hello world\n"


@pytest.mark.asyncio
async def test_fs_edit_non_unique_without_replace_all(workspace):
    p = workspace / "dup.txt"
    p.write_text("aa aa aa\n")
    out = await filesystem.fs_edit({
        "path": str(p),
        "old_string": "aa",
        "new_string": "bb",
    })
    assert "found 3 times" in out
    assert p.read_text() == "aa aa aa\n"


@pytest.mark.asyncio
async def test_fs_edit_replace_all(workspace):
    p = workspace / "dup.txt"
    p.write_text("aa aa aa\n")
    out = await filesystem.fs_edit({
        "path": str(p),
        "old_string": "aa",
        "new_string": "bb",
        "replace_all": True,
    })
    assert "Edited" in out
    assert p.read_text() == "bb bb bb\n"


@pytest.mark.asyncio
async def test_fs_edit_identical_strings_rejected(workspace):
    p = workspace / "f.txt"
    p.write_text("hello\n")
    out = await filesystem.fs_edit({
        "path": str(p),
        "old_string": "hello",
        "new_string": "hello",
    })
    assert "identical" in out


# ---------------------------------------------------------------------------
# fs_delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_delete_removes_file(workspace):
    p = workspace / "doomed.txt"
    p.write_text("x")
    out = await filesystem.fs_delete({"path": str(p)})
    assert "Deleted" in out
    assert not p.exists()


@pytest.mark.asyncio
async def test_fs_delete_refuses_directory(workspace):
    d = workspace / "dir"
    d.mkdir()
    out = await filesystem.fs_delete({"path": str(d)})
    assert "Error" in out
    assert d.exists()


# ---------------------------------------------------------------------------
# Workspace config defaults / multi-root
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_workspaces(tmp_path_factory, monkeypatch):
    ws1 = tmp_path_factory.mktemp("ws1")
    ws2 = tmp_path_factory.mktemp("ws2")
    monkeypatch.setitem(cfg._config, "tool_workspaces", [str(ws1), str(ws2)])
    (ws1 / "a.txt").write_text("a")
    (ws2 / "b.txt").write_text("b")
    out_a = await filesystem.fs_read({"path": str(ws1 / "a.txt")})
    out_b = await filesystem.fs_read({"path": str(ws2 / "b.txt")})
    assert "1\ta" in out_a
    assert "1\tb" in out_b


@pytest.mark.asyncio
async def test_no_workspaces_configured(monkeypatch, tmp_path):
    # Force empty list (overrides default)
    monkeypatch.setitem(cfg._config, "tool_workspaces", [])
    out = await filesystem.fs_read({"path": str(tmp_path / "f.txt")})
    assert "no tool_workspaces configured" in out
