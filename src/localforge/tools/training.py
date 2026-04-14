"""Training pipeline tools — QLoRA fine-tuning via Unsloth integration.

Provides MCP tools to prepare datasets, launch training runs, monitor status,
list completed models, and record feedback for future training data.

Requires an Unsloth environment. Default: ~/Development/unsloth-env/
Override with LOCALFORGE_UNSLOTH_ENV environment variable.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from localforge import config as cfg
from localforge.paths import data_dir
from localforge.tools import tool_handler

log = logging.getLogger("localforge")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_UNSLOTH_ENV = Path(
    os.environ.get(
        "LOCALFORGE_UNSLOTH_ENV",
        os.path.expanduser("~/Development/unsloth-env"),
    )
)
_UNSLOTH_PYTHON = _UNSLOTH_ENV / "bin" / "python"
_TRAIN_SCRIPT = _UNSLOTH_ENV / "train_qlora.py"
_PREPARE_SCRIPT = _UNSLOTH_ENV / "prepare_dataset.py"


def _training_dir() -> Path:
    d = data_dir() / "training"
    d.mkdir(exist_ok=True)
    return d


def _datasets_dir() -> Path:
    d = _training_dir() / "datasets"
    d.mkdir(exist_ok=True)
    return d


def _runs_dir() -> Path:
    d = _training_dir() / "runs"
    d.mkdir(exist_ok=True)
    return d


def _feedback_path() -> Path:
    return _training_dir() / "feedback.jsonl"


# ---------------------------------------------------------------------------
# Active run tracking
# ---------------------------------------------------------------------------
_active_run: dict | None = None  # {name, process, started, status, log_path}


def _check_unsloth() -> str | None:
    """Return an error message if Unsloth env is not available."""
    if not _UNSLOTH_PYTHON.exists():
        return (
            f"Unsloth environment not found at {_UNSLOTH_ENV}\n"
            f"Install: python -m venv {_UNSLOTH_ENV} && "
            f"{_UNSLOTH_ENV}/bin/pip install unsloth\n"
            f"Or set LOCALFORGE_UNSLOTH_ENV to your Unsloth venv path."
        )
    if not _TRAIN_SCRIPT.exists():
        return (
            f"Training script not found at {_TRAIN_SCRIPT}\n"
            f"Expected train_qlora.py in the Unsloth environment root."
        )
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool_handler(
    name="train_prepare",
    description=(
        "Prepare a training dataset from source code or git history. "
        "Modes: 'git-diffs' (commit message training from git log), "
        "'code-pairs' (function-level instruction/completion pairs), "
        "'from-feedback' (export recorded feedback as training data). "
        "Output is a JSONL file ready for train_start."
    ),
    schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["git-diffs", "code-pairs", "from-feedback"],
                "description": "Dataset preparation mode",
            },
            "repo": {
                "type": "string",
                "description": "Git repo path (for git-diffs mode)",
            },
            "directory": {
                "type": "string",
                "description": "Source directory (for code-pairs mode)",
            },
            "glob_pattern": {
                "type": "string",
                "description": "File glob for code-pairs (default: '**/*.py')",
            },
            "name": {
                "type": "string",
                "description": "Dataset name (used as filename). Default: auto-generated.",
            },
            "max_commits": {
                "type": "integer",
                "description": "Max commits for git-diffs (default: 500)",
            },
            "format": {
                "type": "string",
                "enum": ["alpaca", "sharegpt"],
                "description": "Output format for from-feedback mode (default: sharegpt)",
            },
        },
        "required": ["mode"],
    },
)
async def train_prepare(args: dict) -> str:
    mode = args["mode"]

    # from-feedback doesn't need Unsloth, handle separately
    if mode == "from-feedback":
        return await _prepare_from_feedback(args)

    err = _check_unsloth()
    if err:
        return err

    if not _PREPARE_SCRIPT.exists():
        return f"Dataset preparation script not found at {_PREPARE_SCRIPT}"

    name = args.get("name", f"{mode}-{int(time.time())}")
    output_path = _datasets_dir() / f"{name}.jsonl"

    cmd = [str(_UNSLOTH_PYTHON), str(_PREPARE_SCRIPT), mode]

    if mode == "git-diffs":
        repo = args.get("repo")
        if not repo:
            return "Error: 'repo' is required for git-diffs mode"
        repo = os.path.expanduser(repo)
        cmd += ["--repo", repo, "--output", str(output_path)]
        max_commits = args.get("max_commits", 500)
        cmd += ["--max-commits", str(max_commits)]

    elif mode == "code-pairs":
        directory = args.get("directory")
        if not directory:
            return "Error: 'directory' is required for code-pairs mode"
        directory = os.path.expanduser(directory)
        glob_pat = args.get("glob_pattern", "**/*.py")
        cmd += [
            "--directory", directory,
            "--glob", glob_pat,
            "--output", str(output_path),
        ]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return "Dataset preparation timed out after 5 minutes."

    if result.returncode != 0:
        return f"Dataset preparation failed:\n{result.stderr[-1000:]}"

    # Count examples
    try:
        count = sum(1 for line in output_path.read_text().splitlines() if line.strip())
    except Exception:
        count = "unknown"

    return (
        f"Dataset prepared: {output_path.name}\n"
        f"Examples: {count}\n"
        f"Path: {output_path}\n"
        f"Mode: {mode}\n\n"
        f"Next: use train_start to begin training with this dataset."
    )


async def _prepare_from_feedback(args: dict) -> str:
    """Export recorded feedback as training data."""
    fb_path = _feedback_path()
    if not fb_path.exists():
        return "No feedback recorded yet. Use train_feedback to record good/bad responses."

    fmt = args.get("format", "sharegpt")
    name = args.get("name", f"feedback-{int(time.time())}")
    output_path = _datasets_dir() / f"{name}.jsonl"

    entries = []
    for line in fb_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Only use positive feedback for training
        if entry.get("rating", 0) >= 4:
            entries.append(entry)

    if not entries:
        return "No positive feedback (rating >= 4) found to export as training data."

    with open(output_path, "w") as f:
        for entry in entries:
            if fmt == "sharegpt":
                example = {
                    "conversations": [
                        {"from": "human", "value": entry["prompt"]},
                        {"from": "gpt", "value": entry["response"]},
                    ]
                }
            else:  # alpaca
                example = {
                    "instruction": entry["prompt"],
                    "input": entry.get("context", ""),
                    "output": entry["response"],
                }
            f.write(json.dumps(example) + "\n")

    return (
        f"Exported {len(entries)} feedback examples as {fmt} format\n"
        f"Dataset: {output_path}\n"
        f"Next: use train_start to begin training."
    )


@tool_handler(
    name="train_start",
    description=(
        "Start a QLoRA fine-tuning run using Unsloth. "
        "Requires: (1) a dataset from train_prepare, (2) GPU VRAM free (call unload_model first). "
        "Training runs in the background — use train_status to monitor. "
        "Outputs LoRA adapter + optional GGUF export."
    ),
    schema={
        "type": "object",
        "properties": {
            "dataset": {
                "type": "string",
                "description": "Dataset filename (from train_prepare) or full path to JSONL",
            },
            "base_model": {
                "type": "string",
                "description": "HuggingFace model to fine-tune (default: unsloth/Qwen3-8B-bnb-4bit)",
            },
            "name": {
                "type": "string",
                "description": "Run name (used for output directory). Default: auto-generated.",
            },
            "epochs": {
                "type": "integer",
                "description": "Training epochs (default: 3)",
            },
            "batch_size": {
                "type": "integer",
                "description": "Per-device batch size (default: 2, use 1 for large models)",
            },
            "learning_rate": {
                "type": "number",
                "description": "Learning rate (default: 2e-4)",
            },
            "lora_rank": {
                "type": "integer",
                "description": "LoRA rank (default: 16, higher = more capacity but more VRAM)",
            },
            "max_seq_len": {
                "type": "integer",
                "description": "Max sequence length in tokens (default: 2048)",
            },
            "export_gguf": {
                "type": "string",
                "description": "GGUF quant method for export (default: q4_k_m, 'none' to skip)",
            },
        },
        "required": ["dataset"],
    },
)
async def train_start(args: dict) -> str:
    global _active_run

    err = _check_unsloth()
    if err:
        return err

    if _active_run and _active_run.get("process") and _active_run["process"].poll() is None:
        return (
            f"Training already in progress: {_active_run['name']}\n"
            f"Use train_status to check progress, or wait for it to finish."
        )

    # Check if a model is loaded (VRAM conflict)
    if cfg.MODEL:
        return (
            f"Model '{cfg.MODEL}' is currently loaded.\n"
            f"Call unload_model first to free GPU VRAM for training.\n"
            f"Training and inference cannot share the GPU."
        )

    # Resolve dataset path
    dataset = args["dataset"]
    dataset_path = Path(dataset)
    if not dataset_path.is_absolute():
        dataset_path = _datasets_dir() / dataset
        if not dataset_path.exists() and not dataset_path.suffix:
            dataset_path = dataset_path.with_suffix(".jsonl")
    dataset_path = Path(os.path.expanduser(str(dataset_path)))

    if not dataset_path.exists():
        available = [f.name for f in _datasets_dir().glob("*.jsonl")]
        return (
            f"Dataset not found: {dataset_path}\n"
            f"Available datasets: {', '.join(available) or 'none'}\n"
            f"Use train_prepare to create one."
        )

    # Run config
    name = args.get("name", f"run-{int(time.time())}")
    run_dir = _runs_dir() / name
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "training.log"

    base_model = args.get("base_model", "unsloth/Qwen3-8B-bnb-4bit")
    epochs = args.get("epochs", 3)
    batch_size = args.get("batch_size", 2)
    lr = args.get("learning_rate", 2e-4)
    lora_rank = args.get("lora_rank", 16)
    max_seq_len = args.get("max_seq_len", 2048)
    export_gguf = args.get("export_gguf", "q4_k_m")

    cmd = [
        str(_UNSLOTH_PYTHON), str(_TRAIN_SCRIPT),
        "--model", base_model,
        "--dataset", str(dataset_path),
        "--output", str(run_dir / "output"),
        "--epochs", str(epochs),
        "--batch-size", str(batch_size),
        "--lr", str(lr),
        "--lora-rank", str(lora_rank),
        "--max-seq-len", str(max_seq_len),
        "--export-gguf", export_gguf,
    ]

    # Save run config
    run_config = {
        "name": name,
        "base_model": base_model,
        "dataset": str(dataset_path),
        "epochs": epochs,
        "batch_size": batch_size,
        "learning_rate": lr,
        "lora_rank": lora_rank,
        "max_seq_len": max_seq_len,
        "export_gguf": export_gguf,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "running",
    }
    (run_dir / "config.json").write_text(json.dumps(run_config, indent=2))

    # Launch training as background process
    log_file = open(log_path, "w")
    process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(_UNSLOTH_ENV),
    )

    _active_run = {
        "name": name,
        "process": process,
        "started": time.time(),
        "log_path": log_path,
        "log_file": log_file,
        "run_dir": run_dir,
        "config": run_config,
    }

    return (
        f"Training started: {name}\n"
        f"Base model: {base_model}\n"
        f"Dataset: {dataset_path.name} ({sum(1 for _ in dataset_path.read_text().splitlines())} examples)\n"
        f"Epochs: {epochs} | Batch: {batch_size} | LR: {lr} | LoRA rank: {lora_rank}\n"
        f"Output: {run_dir}\n"
        f"Log: {log_path}\n\n"
        f"Use train_status to monitor progress.\n"
        f"After completion, the LoRA adapter and GGUF model will be in the output directory."
    )


@tool_handler(
    name="train_status",
    description=(
        "Check the status of the current or most recent training run. "
        "Shows progress, loss, elapsed time, and output location."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Run name to check. Default: current/most recent run.",
            },
            "tail": {
                "type": "integer",
                "description": "Number of log lines to show (default: 20)",
            },
        },
        "required": [],
    },
)
async def train_status(args: dict) -> str:
    global _active_run

    run_name = args.get("name")
    tail_lines = args.get("tail", 20)

    # If checking a specific run by name
    if run_name:
        run_dir = _runs_dir() / run_name
        if not run_dir.exists():
            return f"Run not found: {run_name}"
        config_path = run_dir / "config.json"
        log_path = run_dir / "training.log"
    elif _active_run:
        run_dir = _active_run["run_dir"]
        config_path = run_dir / "config.json"
        log_path = _active_run["log_path"]
    else:
        # Find most recent run
        runs = sorted(_runs_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not runs:
            return "No training runs found. Use train_start to begin one."
        run_dir = runs[0]
        config_path = run_dir / "config.json"
        log_path = run_dir / "training.log"

    # Load config
    run_config = {}
    if config_path.exists():
        run_config = json.loads(config_path.read_text())

    # Check if actively running
    is_running = False
    if _active_run and _active_run["run_dir"] == run_dir:
        proc = _active_run["process"]
        if proc.poll() is None:
            is_running = True
            elapsed = time.time() - _active_run["started"]
        else:
            # Process finished — update config
            exit_code = proc.returncode
            _active_run["log_file"].close()
            run_config["status"] = "completed" if exit_code == 0 else "failed"
            run_config["exit_code"] = exit_code
            run_config["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            config_path.write_text(json.dumps(run_config, indent=2))
            _active_run = None

    status = "RUNNING" if is_running else run_config.get("status", "unknown").upper()

    lines = [
        f"Run: {run_config.get('name', run_dir.name)}",
        f"Status: {status}",
        f"Base model: {run_config.get('base_model', '?')}",
        f"Dataset: {Path(run_config.get('dataset', '?')).name}",
        f"Epochs: {run_config.get('epochs', '?')} | Batch: {run_config.get('batch_size', '?')} | LoRA rank: {run_config.get('lora_rank', '?')}",
    ]

    if is_running:
        lines.append(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    elif run_config.get("finished_at"):
        lines.append(f"Finished: {run_config['finished_at']}")

    # Show tail of log
    if log_path.exists():
        log_content = log_path.read_text()
        log_lines = log_content.strip().splitlines()

        # Try to extract loss from log
        for line in reversed(log_lines):
            if "'loss'" in line or "loss:" in line.lower() or "training_loss" in line.lower():
                lines.append(f"Latest loss: {line.strip()}")
                break

        lines.append(f"\n── Last {tail_lines} log lines ──")
        for line in log_lines[-tail_lines:]:
            lines.append(line)

    # Check for output artifacts
    output_dir = run_dir / "output"
    if output_dir.exists():
        lora_dir = output_dir / "lora-adapter"
        gguf_dir = output_dir / "gguf"
        if lora_dir.exists():
            lines.append(f"\nLoRA adapter: {lora_dir}")
        if gguf_dir.exists():
            gguf_files = list(gguf_dir.glob("*.gguf"))
            if gguf_files:
                lines.append(f"GGUF model: {gguf_files[0]}")
                lines.append(
                    "To use: symlink to text-generation-webui/user_data/models/ "
                    "and load via swap_model"
                )

    return "\n".join(lines)


@tool_handler(
    name="train_list",
    description="List all training runs, datasets, and available fine-tuned models.",
    schema={
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "enum": ["runs", "datasets", "models", "all"],
                "description": "What to list (default: all)",
            },
        },
        "required": [],
    },
)
async def train_list(args: dict) -> str:
    what = args.get("what", "all")
    sections = []

    if what in ("datasets", "all"):
        datasets = sorted(_datasets_dir().glob("*.jsonl"))
        if datasets:
            lines = ["── Datasets ──"]
            for ds in datasets:
                count = sum(1 for line in ds.read_text().splitlines() if line.strip())
                size_kb = ds.stat().st_size / 1024
                lines.append(f"  {ds.name}  ({count} examples, {size_kb:.0f} KB)")
            sections.append("\n".join(lines))
        elif what == "datasets":
            return "No datasets found. Use train_prepare to create one."

    if what in ("runs", "all"):
        runs = sorted(_runs_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        runs = [r for r in runs if r.is_dir()]
        if runs:
            lines = ["── Training Runs ──"]
            for run_dir in runs[:20]:
                config_path = run_dir / "config.json"
                if config_path.exists():
                    rc = json.loads(config_path.read_text())
                    status = rc.get("status", "unknown")
                    base = rc.get("base_model", "?").split("/")[-1]
                    started = rc.get("started_at", "?")
                    lines.append(f"  {run_dir.name}  [{status}]  {base}  ({started})")
                else:
                    lines.append(f"  {run_dir.name}  [unknown]")
            sections.append("\n".join(lines))
        elif what == "runs":
            return "No training runs found. Use train_start to begin one."

    if what in ("models", "all"):
        # Find GGUF outputs from completed runs
        models = []
        for run_dir in _runs_dir().iterdir():
            if not run_dir.is_dir():
                continue
            gguf_dir = run_dir / "output" / "gguf"
            if gguf_dir.exists():
                for gguf in gguf_dir.glob("*.gguf"):
                    models.append((gguf, run_dir.name))
        if models:
            lines = ["── Fine-tuned Models (GGUF) ──"]
            for gguf_path, run_name in models:
                size_mb = gguf_path.stat().st_size / (1024 * 1024)
                lines.append(f"  {gguf_path.name}  ({size_mb:.0f} MB)  from run: {run_name}")
            sections.append("\n".join(lines))
        elif what == "models":
            return "No fine-tuned GGUF models found. Complete a training run first."

    # Feedback stats
    if what == "all":
        fb_path = _feedback_path()
        if fb_path.exists():
            fb_lines = [line for line in fb_path.read_text().splitlines() if line.strip()]
            positive = sum(1 for line in fb_lines if json.loads(line).get("rating", 0) >= 4)
            sections.append(
                f"── Feedback ──\n"
                f"  Total: {len(fb_lines)} entries ({positive} positive, usable for training)"
            )

    if not sections:
        return (
            "No training data found.\n\n"
            "Getting started:\n"
            "  1. train_prepare(mode='git-diffs', repo='~/my-project') — build dataset\n"
            "  2. unload_model() — free GPU VRAM\n"
            "  3. train_start(dataset='my-dataset.jsonl') — start training\n"
            "  4. train_status() — monitor progress"
        )

    return "\n\n".join(sections)


@tool_handler(
    name="train_feedback",
    description=(
        "Record a response as good or bad for future training data. "
        "Good responses (rating >= 4) can be exported as training datasets via "
        "train_prepare(mode='from-feedback'). This builds a personalized training pipeline."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The prompt/instruction that was given",
            },
            "response": {
                "type": "string",
                "description": "The model's response to rate",
            },
            "rating": {
                "type": "integer",
                "description": "Quality rating 1-5 (1=terrible, 3=ok, 5=excellent)",
            },
            "context": {
                "type": "string",
                "description": "Optional context (code being reviewed, file path, etc.)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for categorization (e.g. 'code-review', 'explanation')",
            },
            "model": {
                "type": "string",
                "description": "Model that generated the response. Default: currently loaded model.",
            },
        },
        "required": ["prompt", "response", "rating"],
    },
)
async def train_feedback(args: dict) -> str:
    rating = args["rating"]
    if not 1 <= rating <= 5:
        return "Rating must be between 1 and 5."

    entry = {
        "prompt": args["prompt"],
        "response": args["response"],
        "rating": rating,
        "context": args.get("context", ""),
        "tags": args.get("tags", []),
        "model": args.get("model", cfg.MODEL or "unknown"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    fb_path = _feedback_path()
    with open(fb_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    total = sum(1 for line in fb_path.read_text().splitlines() if line.strip())
    quality = "positive" if rating >= 4 else "negative" if rating <= 2 else "neutral"

    return (
        f"Feedback recorded ({quality}, {rating}/5)\n"
        f"Total feedback entries: {total}\n"
        f"Use train_prepare(mode='from-feedback') to export positive entries as training data."
    )
