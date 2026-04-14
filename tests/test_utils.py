"""Tests for tools/utils.py shared utilities."""

import os
from pathlib import Path

from localforge.tools.utils import (
    error_response,
    validate_directory,
    validate_file_path,
)


def test_validate_file_path_valid(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    path, err = validate_file_path(str(f))
    assert err is None
    assert path == f


def test_validate_file_path_not_found(tmp_path):
    _, err = validate_file_path(str(tmp_path / "nope.txt"))
    assert err is not None
    assert "not found" in err


def test_validate_file_path_too_large(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 200)
    _, err = validate_file_path(str(f), max_size=100)
    assert err is not None
    assert "too large" in err


def test_validate_file_path_directory(tmp_path):
    _, err = validate_file_path(str(tmp_path))
    assert err is not None
    assert "not a file" in err


def test_validate_directory_valid(tmp_path):
    path, err = validate_directory(str(tmp_path))
    assert err is None
    assert path == tmp_path


def test_validate_directory_not_found(tmp_path):
    _, err = validate_directory(str(tmp_path / "nope"))
    assert err is not None
    assert "not found" in err


def test_validate_directory_is_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hi")
    _, err = validate_directory(str(f))
    assert err is not None
    assert "not a directory" in err


def test_error_response():
    resp = error_response("something broke", 500)
    assert resp["error"] == "something broke"
    assert resp["status"] == 500


def test_error_response_custom_status():
    resp = error_response("not found", 404)
    assert resp["status"] == 404
