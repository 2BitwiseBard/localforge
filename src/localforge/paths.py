"""Path resolution for LocalForge data directories.

All data directories are resolved from LOCALFORGE_DATA_DIR environment variable,
defaulting to ~/.local/share/localforge. This ensures the project works
standalone (not just inside ~/.claude/mcp-servers/).
"""

import os
from pathlib import Path

_DATA_DIR: Path | None = None


def data_dir() -> Path:
    """Root data directory for all LocalForge runtime data."""
    global _DATA_DIR
    if _DATA_DIR is None:
        env = os.environ.get("LOCALFORGE_DATA_DIR")
        if env:
            _DATA_DIR = Path(os.path.expanduser(env))
        else:
            _DATA_DIR = Path(os.path.expanduser("~/.local/share/localforge"))
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


def notes_dir() -> Path:
    d = data_dir() / "notes"
    d.mkdir(exist_ok=True)
    return d


def indexes_dir() -> Path:
    d = data_dir() / "indexes"
    d.mkdir(exist_ok=True)
    return d


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(exist_ok=True)
    return d


def pipelines_dir() -> Path:
    d = data_dir() / "pipelines"
    d.mkdir(exist_ok=True)
    return d


def vectors_dir() -> Path:
    d = data_dir() / "vectors"
    d.mkdir(exist_ok=True)
    return d


def photos_dir(user: str = "default") -> Path:
    d = data_dir() / "photos" / user
    d.mkdir(parents=True, exist_ok=True)
    return d


def chats_dir(user: str = "default") -> Path:
    d = data_dir() / "chats" / user
    d.mkdir(parents=True, exist_ok=True)
    return d


def agent_state_dir() -> Path:
    d = data_dir() / "agent_state"
    d.mkdir(exist_ok=True)
    return d


def knowledge_db_path() -> Path:
    return data_dir() / "knowledge.db"


def task_queue_db_path() -> Path:
    return data_dir() / "task_queue.db"


def approval_db_path() -> Path:
    return data_dir() / "approval_queue.db"


def message_bus_db_path() -> Path:
    return data_dir() / "message_bus.db"


def config_path() -> Path:
    """Resolve config.yaml location.

    Priority:
    1. LOCALFORGE_CONFIG environment variable
    2. config.yaml next to server.py (src/localforge/config.yaml)
    3. ~/.config/localforge/config.yaml
    """
    env = os.environ.get("LOCALFORGE_CONFIG")
    if env:
        return Path(os.path.expanduser(env))

    # Next to the source code
    src_config = Path(__file__).parent / "config.yaml"
    if src_config.exists():
        return src_config

    # XDG config home
    xdg = Path(os.path.expanduser("~/.config/localforge/config.yaml"))
    if xdg.exists():
        return xdg

    # Default to src location (even if it doesn't exist yet)
    return src_config


def training_dir() -> Path:
    """Root directory for all training data (datasets, runs, feedback).

    Override with LOCALFORGE_TRAINING_DIR to put training data outside the
    main data directory — useful when sharing datasets across tools or storing
    them on a separate drive.

    Default: ~/Development/training  (accessible from scripts, services, Claude Code)
    """
    env = os.environ.get("LOCALFORGE_TRAINING_DIR")
    if env:
        d = Path(os.path.expanduser(env))
    else:
        d = Path(os.path.expanduser("~/Development/training"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def agents_config_path() -> Path:
    """Resolve agents.yaml location (same logic as config_path)."""
    env = os.environ.get("LOCALFORGE_AGENTS_CONFIG")
    if env:
        return Path(os.path.expanduser(env))

    # Next to config.yaml
    cfg = config_path()
    agents_yaml = cfg.parent / "agents.yaml"
    if agents_yaml.exists():
        return agents_yaml

    # Default to same directory as config (even if it doesn't exist yet)
    return agents_yaml


def fastembed_cache_dir() -> Path:
    return Path(os.path.expanduser("~/.cache/fastembed"))
