"""Tests for database path resolution via paths.py."""

from localforge.paths import (
    approval_db_path,
    knowledge_db_path,
    message_bus_db_path,
    task_queue_db_path,
)


def test_all_db_paths_under_data_dir(tmp_data_dir):
    """All database paths should resolve under LOCALFORGE_DATA_DIR."""
    for path_fn in [knowledge_db_path, task_queue_db_path, approval_db_path, message_bus_db_path]:
        db_path = path_fn()
        assert str(db_path).startswith(str(tmp_data_dir)), (
            f"{path_fn.__name__}() returned {db_path}, expected under {tmp_data_dir}"
        )
        assert db_path.name.endswith(".db")


def test_db_paths_are_distinct(tmp_data_dir):
    """Each database should have a unique path."""
    paths = {
        knowledge_db_path(),
        task_queue_db_path(),
        approval_db_path(),
        message_bus_db_path(),
    }
    assert len(paths) == 4, "Database paths should be unique"
