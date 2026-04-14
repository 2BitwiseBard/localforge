"""Hardware auto-detection for compute mesh worker agents.

Detects GPU, RAM, CPU, and available capabilities.
Supports: NVIDIA, Apple Silicon, Adreno (Android), AMD Radeon, Vulkan fallback.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional


# Adreno GPU → estimated usable VRAM (MB) for inference
_ADRENO_VRAM: dict[str, int] = {
    "530": 1536, "540": 2048,
    "612": 1536, "615": 2048, "616": 2048, "618": 2048,
    "619": 2048, "620": 2048, "630": 2048, "640": 3072,
    "642L": 2048, "650": 3072, "660": 3072,
    "730": 4096, "740": 4096, "750": 4096,
    "830": 4096,
}

# Model recommendations by device class: (filename, size_gb)
_MODEL_RECOMMENDATIONS: list[tuple[dict, str, float]] = [
    # (match_criteria, filename, size_gb)
    # Checked in order — first match wins
]


@dataclass
class HardwareInfo:
    """Detected hardware capabilities."""
    platform: str = ""
    gpu_type: str = "none"         # nvidia, apple_silicon, adreno, amd, none
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
    # Thermal/battery (mobile + laptop)
    battery_pct: int = -1          # -1 = not available
    battery_charging: bool = False
    thermal_throttled: bool = False

    def tier(self) -> str:
        if self.gpu_type == "nvidia" and self.vram_mb >= 12000:
            return "gpu-primary"
        if self.gpu_type in ("nvidia", "apple_silicon", "amd") and self.vram_mb >= 4000:
            return "gpu-secondary"
        if self.gpu_type == "adreno" and self.ram_mb >= 8000:
            return "cpu-capable"
        if self.ram_mb >= 8000:
            return "cpu-capable"
        return "lightweight"

    def recommended_model(self) -> Optional[tuple[str, float]]:
        """Return (filename, size_gb) for the best model this device can run.

        Returns None if the device can't realistically run inference.
        """
        if self.gpu_type == "adreno" or (self.platform == "android" and self.ram_mb <= 8000):
            return ("Qwen3.5-2B-UD-Q8_K_XL.gguf", 2.8)
        # 8GB+ VRAM or Apple Silicon 16GB+ → 9B model
        if self.vram_mb >= 8000 or (self.gpu_type == "apple_silicon" and self.ram_mb >= 16000):
            return ("Qwen3.5-9B-UD-Q4_K_XL.gguf", 6.0)
        # 4-8GB VRAM or 12GB+ RAM → 4B model
        if self.vram_mb >= 4000 or self.ram_mb >= 12000:
            return ("Qwen3.5-4B-UD-Q6_K_XL.gguf", 4.1)
        if self.ram_mb >= 8000:
            return ("Qwen3.5-4B-UD-Q6_K_XL.gguf", 4.1)
        if self.ram_mb >= 4000:
            return ("Qwen3.5-2B-UD-Q8_K_XL.gguf", 2.8)
        return None

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
            "battery_pct": self.battery_pct,
            "battery_charging": self.battery_charging,
            "thermal_throttled": self.thermal_throttled,
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

    # --- Android / Termux ---
    if os.path.exists("/data/data/com.termux"):
        info.platform = "android"

    # --- RAM ---
    try:
        if info.platform in ("linux", "android"):
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

    # --- Adreno GPU (Qualcomm Android) ---
    if info.gpu_type == "none" and info.platform == "android":
        try:
            gpu_model = ""
            # Try kernel sysfs (works on most Qualcomm devices)
            sysfs = "/sys/class/kgsl/kgsl-3d0/gpu_model"
            if os.path.exists(sysfs):
                with open(sysfs) as f:
                    gpu_model = f.read().strip()
            # Fallback: Android property
            if not gpu_model and shutil.which("getprop"):
                result = subprocess.run(
                    ["getprop", "ro.hardware.chipname"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    gpu_model = f"Adreno ({result.stdout.strip()})"

            if gpu_model:
                info.gpu_type = "adreno"
                info.gpu_name = gpu_model
                # Extract Adreno model number for VRAM lookup
                for model_num, vram in _ADRENO_VRAM.items():
                    if model_num in gpu_model:
                        info.vram_mb = vram
                        break
                if info.vram_mb == 0:
                    info.vram_mb = 2048  # Conservative default
                info.inference = True
                info.max_model_params = max(1, info.ram_mb // 2000)
        except Exception:
            pass

    # --- AMD Radeon ---
    if info.gpu_type == "none":
        try:
            if sys.platform == "darwin":
                # macOS: system_profiler for AMD GPUs (Intel Macs)
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType", "-json"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    import json
                    sp_data = json.loads(result.stdout)
                    for display in sp_data.get("SPDisplaysDataType", []):
                        chipset = display.get("sppci_model", "")
                        if "AMD" in chipset or "Radeon" in chipset:
                            info.gpu_type = "amd"
                            info.gpu_name = chipset
                            vram_str = display.get("spdisplays_vram", "0")
                            # Parse "4 GB" or "4096 MB" style
                            vram_str = vram_str.lower().replace(",", "")
                            if "gb" in vram_str:
                                info.vram_mb = int(float(vram_str.split()[0]) * 1024)
                            elif "mb" in vram_str:
                                info.vram_mb = int(float(vram_str.split()[0]))
                            info.inference = True
                            info.vision = info.vram_mb >= 8000
                            info.max_model_params = _estimate_max_params(info.vram_mb)
                            break
            else:
                # Linux: check DRM subsystem for AMD vendor ID 0x1002
                drm_base = "/sys/class/drm"
                if os.path.isdir(drm_base):
                    for card_dir in sorted(os.listdir(drm_base)):
                        vendor_path = os.path.join(drm_base, card_dir, "device", "vendor")
                        if os.path.exists(vendor_path):
                            with open(vendor_path) as f:
                                if "0x1002" in f.read():
                                    info.gpu_type = "amd"
                                    # Try to get GPU name
                                    uevent = os.path.join(drm_base, card_dir, "device", "uevent")
                                    if os.path.exists(uevent):
                                        with open(uevent) as f:
                                            for line in f:
                                                if "PCI_SLOT_NAME" in line:
                                                    info.gpu_name = f"AMD Radeon ({line.split('=')[1].strip()})"
                                    # Try rocm-smi for VRAM
                                    if shutil.which("rocm-smi"):
                                        result = subprocess.run(
                                            ["rocm-smi", "--showmeminfo", "vram", "--json"],
                                            capture_output=True, text=True,
                                        )
                                        if result.returncode == 0:
                                            import json
                                            rocm = json.loads(result.stdout)
                                            for card in rocm.values():
                                                if isinstance(card, dict):
                                                    total = card.get("VRAM Total Memory (B)", 0)
                                                    if total:
                                                        info.vram_mb = int(total) // (1024 * 1024)
                                    info.inference = True
                                    info.vision = info.vram_mb >= 8000
                                    if info.vram_mb:
                                        info.max_model_params = _estimate_max_params(info.vram_mb)
                                    break
        except Exception:
            pass

    # --- Vulkan fallback ---
    if info.gpu_type == "none" and shutil.which("vulkaninfo"):
        try:
            result = subprocess.run(
                ["vulkaninfo", "--summary"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                output = result.stdout
                for line in output.splitlines():
                    line = line.strip()
                    if "deviceName" in line and "=" in line:
                        info.gpu_name = line.split("=", 1)[1].strip()
                    elif "maxMemoryAllocationSize" in line or "heapSize" in line:
                        # Try to extract memory size
                        for word in line.split():
                            try:
                                val = int(word)
                                if val > 1_000_000_000:  # bytes
                                    info.vram_mb = max(info.vram_mb, val // (1024 * 1024))
                                elif val > 1_000_000:  # already MB
                                    info.vram_mb = max(info.vram_mb, val)
                            except ValueError:
                                continue
                if info.gpu_name:
                    info.gpu_type = "vulkan"
                    info.inference = True
                    if info.vram_mb:
                        info.max_model_params = _estimate_max_params(info.vram_mb)
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

    # --- Thermal / Battery ---
    _detect_thermal_battery(info)

    return info


def _detect_thermal_battery(info: HardwareInfo) -> None:
    """Populate battery and thermal fields."""
    # Linux / Android battery
    for bat_path in ["/sys/class/power_supply/battery",
                     "/sys/class/power_supply/BAT0",
                     "/sys/class/power_supply/BAT1"]:
        cap_file = os.path.join(bat_path, "capacity")
        status_file = os.path.join(bat_path, "status")
        if os.path.exists(cap_file):
            try:
                with open(cap_file) as f:
                    info.battery_pct = int(f.read().strip())
                if os.path.exists(status_file):
                    with open(status_file) as f:
                        info.battery_charging = f.read().strip().lower() in ("charging", "full")
            except Exception:
                pass
            break

    # macOS battery
    if sys.platform == "darwin" and info.battery_pct == -1:
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                output = result.stdout
                # Parse "InternalBattery-0 (id=...)	85%; charging;"
                for line in output.splitlines():
                    if "InternalBattery" in line:
                        parts = line.split("\t")
                        if len(parts) >= 2:
                            pct_str = parts[1].split("%")[0].strip()
                            try:
                                info.battery_pct = int(pct_str)
                            except ValueError:
                                pass
                            info.battery_charging = "charging" in parts[1].lower()
        except Exception:
            pass

    # Thermal throttling (Linux / Android)
    thermal_zones = "/sys/class/thermal"
    if os.path.isdir(thermal_zones):
        try:
            for tz in os.listdir(thermal_zones):
                temp_file = os.path.join(thermal_zones, tz, "temp")
                if os.path.exists(temp_file):
                    with open(temp_file) as f:
                        temp_mc = int(f.read().strip())  # millidegrees C
                        if temp_mc > 45000:  # >45°C
                            info.thermal_throttled = True
                            break
        except Exception:
            pass


if __name__ == "__main__":
    import json
    hw = detect()
    print(f"Tier: {hw.tier()}")
    rec = hw.recommended_model()
    if rec:
        print(f"Recommended model: {rec[0]} ({rec[1]} GB)")
    print(json.dumps(hw.to_dict(), indent=2))
