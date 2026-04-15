"""Cross-platform detection tests for localforge.workers.detect.

Monkeypatches sys.platform + subprocess.run so the same Linux host can
exercise the Windows/macOS/Android code paths.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from localforge.workers import detect


def _base_info():
    """Return a fresh HardwareInfo the way detect.detect() starts."""
    info = detect.HardwareInfo()
    info.cpu_cores = 4
    return info


class TestTierAndRecommendations:
    def test_gpu_primary_tier(self):
        info = _base_info()
        info.gpu_type = "nvidia"
        info.vram_mb = 16000
        assert info.tier() == "gpu-primary"

    def test_gpu_secondary_tier_apple_silicon(self):
        info = _base_info()
        info.gpu_type = "apple_silicon"
        info.vram_mb = 8000
        info.ram_mb = 16000
        assert info.tier() == "gpu-secondary"

    def test_cpu_capable_tier_adreno(self):
        info = _base_info()
        info.gpu_type = "adreno"
        info.ram_mb = 8000
        assert info.tier() == "cpu-capable"

    def test_lightweight_tier_small_ram(self):
        info = _base_info()
        info.ram_mb = 4000
        assert info.tier() == "lightweight"

    def test_recommended_model_adreno_small(self):
        info = _base_info()
        info.gpu_type = "adreno"
        info.platform = "android"
        info.ram_mb = 6000
        assert info.recommended_model() == ("Qwen3.5-2B-UD-Q8_K_XL.gguf", 2.8)

    def test_recommended_model_nvidia_8gb(self):
        info = _base_info()
        info.vram_mb = 12000
        rec = info.recommended_model()
        assert rec and rec[0] == "Qwen3.5-9B-UD-Q4_K_XL.gguf"


class TestCrossPlatformDetection:
    """Each test isolates a platform via patches and verifies the
    corresponding branch sets reasonable fields."""

    def test_android_platform_via_termux_env(self, monkeypatch):
        monkeypatch.setenv("TERMUX_VERSION", "0.118.1")
        # Don't rely on /data/data/com.termux existing on the test host.
        with patch("localforge.workers.detect.subprocess.run") as run:
            # Simulate nvidia-smi missing, sysctl missing
            run.side_effect = FileNotFoundError()
            with patch("localforge.workers.detect.os.path.exists", return_value=False), \
                 patch("localforge.workers.detect.shutil.which", return_value=None), \
                 patch("localforge.workers.detect.os.path.isdir", return_value=False):
                info = detect.detect()
        assert info.platform == "android"

    def test_windows_platform_fallback_to_cpu(self):
        """On Windows with no GPU, we should still report inference=True if RAM >= 4GB."""
        fake_vm = MagicMock()
        fake_vm.total = 16 * 1024 * 1024 * 1024  # 16 GB
        with patch("localforge.workers.detect.sys") as sysmod, \
             patch("localforge.workers.detect.psutil") as ps, \
             patch("localforge.workers.detect._HAS_PSUTIL", True), \
             patch("localforge.workers.detect.subprocess.run") as run, \
             patch("localforge.workers.detect.shutil.which", return_value=None), \
             patch("localforge.workers.detect.os.path.exists", return_value=False), \
             patch("localforge.workers.detect.os.path.isdir", return_value=False), \
             patch("localforge.workers.detect.os.cpu_count", return_value=8):
            sysmod.platform = "win32"
            ps.virtual_memory.return_value = fake_vm
            ps.sensors_battery.return_value = None
            ps.sensors_temperatures.return_value = {}
            run.side_effect = FileNotFoundError()  # nvidia-smi + sysctl absent
            info = detect.detect()
        assert info.platform == "win32"
        assert info.ram_mb == 16 * 1024  # MB
        assert info.inference is True  # CPU fallback
        assert info.cpu_cores == 8

    def test_macos_apple_silicon_sets_mlx(self):
        """Apple Silicon branch should populate mlx_available based on the
        module spec check."""
        fake_vm = MagicMock()
        fake_vm.total = 32 * 1024 * 1024 * 1024

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "machdep.cpu.brand_string" in cmd:
                result.stdout = "Apple M2 Max\n"
            elif "hw.memsize" in cmd:
                result.stdout = str(32 * 1024 * 1024 * 1024)
            elif "pmset" in cmd:
                result.stdout = ""
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        # Simulate MLX being installed via find_spec
        fake_spec = MagicMock()
        with patch("localforge.workers.detect.sys") as sysmod, \
             patch("localforge.workers.detect.psutil") as ps, \
             patch("localforge.workers.detect._HAS_PSUTIL", True), \
             patch("localforge.workers.detect.subprocess.run", side_effect=fake_run), \
             patch("localforge.workers.detect.shutil.which", return_value=None), \
             patch("localforge.workers.detect.os.path.exists", return_value=False), \
             patch("localforge.workers.detect.os.path.isdir", return_value=False), \
             patch("localforge.workers.detect.importlib.util.find_spec", return_value=fake_spec), \
             patch("localforge.workers.detect.os.cpu_count", return_value=12):
            sysmod.platform = "darwin"
            ps.virtual_memory.return_value = fake_vm
            ps.sensors_battery.return_value = None
            ps.sensors_temperatures.return_value = {}
            info = detect.detect()

        assert info.platform == "darwin"
        assert info.gpu_type == "apple_silicon"
        assert info.mlx_available is True
        assert info.inference is True
        assert info.vram_mb == info.ram_mb  # unified memory

    def test_linux_ram_psutil_first(self):
        """psutil path should populate ram_mb without reading /proc."""
        fake_vm = MagicMock()
        fake_vm.total = 8 * 1024 * 1024 * 1024
        with patch("localforge.workers.detect.sys") as sysmod, \
             patch("localforge.workers.detect.psutil") as ps, \
             patch("localforge.workers.detect._HAS_PSUTIL", True), \
             patch("localforge.workers.detect.subprocess.run") as run, \
             patch("localforge.workers.detect.shutil.which", return_value=None), \
             patch("localforge.workers.detect.os.path.exists", return_value=False), \
             patch("localforge.workers.detect.os.path.isdir", return_value=False), \
             patch("localforge.workers.detect.os.cpu_count", return_value=4):
            sysmod.platform = "linux"
            ps.virtual_memory.return_value = fake_vm
            ps.sensors_battery.return_value = None
            ps.sensors_temperatures.return_value = {}
            run.side_effect = FileNotFoundError()
            info = detect.detect()
        assert info.ram_mb == 8 * 1024


class TestToDictRoundtrip:
    def test_to_dict_contains_new_mlx_field(self):
        info = _base_info()
        info.mlx_available = True
        d = info.to_dict()
        assert d["mlx_available"] is True
        assert "platform" in d
        assert "vram_mb" in d
