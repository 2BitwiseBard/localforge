"""Hardware auto-detection for compute mesh worker agents.

Detects GPU, RAM, CPU, and available capabilities.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class HardwareInfo:
    """Detected hardware capabilities."""
    platform: str = ""
    gpu_type: str = "none"         # nvidia, apple_silicon, adreno, none
    gpu_name: str = ""
    vram_mb: int = 0
    ram_mb: int = 0
    cpu_cores: int = 0
    # Capabilities
    inference: bool = False
    embeddings: bool = True        # Always true (CPU fastembed)
    reranking: bool = True         # Always true (CPU cross-encoder)
    classification: bool = True    # Always true (small models)
    tts: bool = False
    stt: bool = False
    vision: bool = False
    max_model_params: int = 0      # Estimated max model size (B)

    def tier(self) -> str:
        if self.gpu_type == "nvidia" and self.vram_mb >= 12000:
            return "gpu-primary"
        if self.gpu_type in ("nvidia", "apple_silicon") and self.vram_mb >= 4000:
            return "gpu-secondary"
        if self.ram_mb >= 8000:
            return "cpu-capable"
        return "lightweight"

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "gpu_type": self.gpu_type,
            "gpu_name": self.gpu_name,
            "vram_mb": self.vram_mb,
            "ram_mb": self.ram_mb,
            "cpu_cores": self.cpu_cores,
            "inference": self.inference,
            "embeddings": self.embeddings,
            "reranking": self.reranking,
            "classification": self.classification,
            "tts": self.tts,
            "stt": self.stt,
            "vision": self.vision,
            "max_model_params": self.max_model_params,
        }


def _estimate_max_params(vram_mb: int) -> int:
    """Estimate max model params (B) from VRAM in MB.

    Rough heuristic for Q4 quantized GGUF models:
    ~0.5-0.6 GB per billion parameters at Q4.
    """
    usable = vram_mb * 0.85  # Reserve 15% for KV cache
    return int(usable / 600)  # ~600 MB per B at Q4


def _has_package(name: str) -> bool:
    """Check if a Python package is importable."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def detect() -> HardwareInfo:
    """Auto-detect hardware capabilities."""
    info = HardwareInfo()
    info.platform = sys.platform
    info.cpu_cores = os.cpu_count() or 1

    # --- RAM ---
    try:
        if sys.platform == "linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        info.ram_mb = int(line.split()[1]) // 1024
                        break
        elif sys.platform == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                info.ram_mb = int(result.stdout.strip()) // (1024 * 1024)
    except Exception:
        pass

    # --- NVIDIA GPU ---
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(", ")
            if len(parts) >= 2:
                info.gpu_type = "nvidia"
                info.gpu_name = parts[0]
                info.vram_mb = int(float(parts[1]))
                info.inference = True
                info.vision = info.vram_mb >= 8000
                info.max_model_params = _estimate_max_params(info.vram_mb)
    except FileNotFoundError:
        pass

    # --- Apple Silicon ---
    if sys.platform == "darwin" and info.gpu_type == "none":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and "Apple" in result.stdout:
                info.gpu_type = "apple_silicon"
                info.gpu_name = result.stdout.strip()
                # Apple Silicon uses unified memory
                info.vram_mb = info.ram_mb
                info.inference = True
                info.vision = info.ram_mb >= 16000
                info.max_model_params = _estimate_max_params(int(info.ram_mb * 0.7))
        except Exception:
            pass

    # --- CPU-only inference ---
    if not info.inference and info.ram_mb >= 4000:
        # Can still run small models on CPU via llama.cpp
        info.inference = True
        info.max_model_params = max(1, info.ram_mb // 2000)  # Very rough

    # --- TTS/STT ---
    info.tts = (shutil.which("piper") is not None or
                _has_package("TTS") or
                _has_package("piper"))
    info.stt = (_has_package("whisper") or
                _has_package("faster_whisper"))

    return info


if __name__ == "__main__":
    import json
    hw = detect()
    print(f"Tier: {hw.tier()}")
    print(json.dumps(hw.to_dict(), indent=2))
