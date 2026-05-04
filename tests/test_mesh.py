"""Tests for compute mesh heartbeat registry, worker detection, and routing."""

import time


class TestMeshWorkerRegistry:
    def setup_method(self):
        from localforge.gpu_pool import GPUPool
        self.pool = GPUPool({})
        self.pool._heartbeat_nodes.clear()

    def test_heartbeat_registers_worker(self):
        key, accepted = self.pool.register_heartbeat({
            "hostname": "laptop2",
            "port": 8200,
            "tier": "gpu-secondary",
            "capabilities": {"inference": True, "embeddings": True},
            "model_name": "Qwen3.5-4B-UD-Q6_K_XL.gguf",
            "active_tasks": 0,
            "stats": {"tasks_completed": 5, "tasks_failed": 0},
            "uptime_s": 3600,
        })
        assert accepted
        assert key == "laptop2:8200"
        assert "laptop2:8200" in self.pool._heartbeat_nodes
        assert self.pool._heartbeat_nodes["laptop2:8200"]["tier"] == "gpu-secondary"
        assert self.pool._heartbeat_nodes["laptop2:8200"]["model_name"] == "Qwen3.5-4B-UD-Q6_K_XL.gguf"

    def test_multiple_workers(self):
        self.pool.register_heartbeat({"hostname": "laptop2", "port": 8200})
        self.pool.register_heartbeat({"hostname": "phone", "port": 8200})
        assert len(self.pool._heartbeat_nodes) == 2

    def test_worker_overwrite_on_reheart(self):
        self.pool.register_heartbeat({"hostname": "laptop2", "port": 8200, "active_tasks": 1})
        self.pool.register_heartbeat({"hostname": "laptop2", "port": 8200, "active_tasks": 0})
        assert self.pool._heartbeat_nodes["laptop2:8200"]["active_tasks"] == 0

    def test_size_bound_rejects_new(self):
        self.pool._max_heartbeat_nodes = 2
        self.pool.register_heartbeat({"hostname": "a", "port": 8200})
        self.pool.register_heartbeat({"hostname": "b", "port": 8200})
        _, accepted = self.pool.register_heartbeat({"hostname": "c", "port": 8200})
        assert not accepted
        assert len(self.pool._heartbeat_nodes) == 2

    def test_size_bound_allows_existing_update(self):
        self.pool._max_heartbeat_nodes = 2
        self.pool.register_heartbeat({"hostname": "a", "port": 8200, "active_tasks": 1})
        self.pool.register_heartbeat({"hostname": "b", "port": 8200})
        _, accepted = self.pool.register_heartbeat({"hostname": "a", "port": 8200, "active_tasks": 0})
        assert accepted
        assert self.pool._heartbeat_nodes["a:8200"]["active_tasks"] == 0

    def test_get_mesh_workers_returns_health(self):
        self.pool.register_heartbeat({"hostname": "fresh", "port": 8200})
        workers = self.pool.get_mesh_workers()
        assert len(workers) == 1
        assert workers[0]["healthy"] is True

    def test_get_mesh_workers_cleans_stale(self):
        self.pool._heartbeat_nodes["stale:8200"] = {
            "hostname": "stale", "port": 8200,
            "last_heartbeat": time.time() - 700,  # >10 min ago
        }
        workers = self.pool.get_mesh_workers()
        assert len(workers) == 0
        assert "stale:8200" not in self.pool._heartbeat_nodes


class TestDetectHardware:
    def test_detect_returns_hardware_info(self):
        from localforge.workers.detect import HardwareInfo, detect
        hw = detect()
        assert isinstance(hw, HardwareInfo)
        assert hw.cpu_cores > 0
        assert hw.ram_mb > 0
        assert hw.platform != ""

    def test_tier_classification(self):
        from localforge.workers.detect import HardwareInfo
        # GPU primary
        hw = HardwareInfo(gpu_type="nvidia", vram_mb=16000, ram_mb=32000)
        assert hw.tier() == "gpu-primary"
        # GPU secondary
        hw = HardwareInfo(gpu_type="nvidia", vram_mb=6000, ram_mb=16000)
        assert hw.tier() == "gpu-secondary"
        # CPU capable
        hw = HardwareInfo(gpu_type="none", vram_mb=0, ram_mb=16000)
        assert hw.tier() == "cpu-capable"
        # Lightweight
        hw = HardwareInfo(gpu_type="none", vram_mb=0, ram_mb=2000)
        assert hw.tier() == "lightweight"

    def test_tier_adreno_phone(self):
        """Adreno 660, 12GB RAM → cpu-capable."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(platform="android", gpu_type="adreno", gpu_name="Adreno 660",
                          vram_mb=3072, ram_mb=12000)
        assert hw.tier() == "cpu-capable"

    def test_tier_amd_radeon(self):
        """AMD Radeon 8GB → gpu-secondary."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(gpu_type="amd", vram_mb=8192, ram_mb=16000)
        assert hw.tier() == "gpu-secondary"

    def test_tier_nvidia_small(self):
        """NVIDIA 4GB → gpu-secondary."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(gpu_type="nvidia", vram_mb=4096, ram_mb=16000)
        assert hw.tier() == "gpu-secondary"

    def test_tier_lightweight(self):
        """4GB RAM, no GPU → lightweight."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(gpu_type="none", vram_mb=0, ram_mb=4000)
        assert hw.tier() == "lightweight"

    def test_to_dict(self):
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(platform="linux", gpu_type="nvidia", gpu_name="RTX 3080", vram_mb=16000)
        d = hw.to_dict()
        assert d["platform"] == "linux"
        assert d["gpu_name"] == "RTX 3080"
        assert d["vram_mb"] == 16000
        assert isinstance(d["inference"], bool)
        # New fields present
        assert "battery_pct" in d
        assert "battery_charging" in d
        assert "thermal_throttled" in d

    def test_to_dict_thermal_battery(self):
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(battery_pct=85, battery_charging=True, thermal_throttled=False)
        d = hw.to_dict()
        assert d["battery_pct"] == 85
        assert d["battery_charging"] is True
        assert d["thermal_throttled"] is False

    def test_max_params_estimate(self):
        from localforge.workers.detect import _estimate_max_params
        # 16GB VRAM -> roughly 22B at Q4
        params = _estimate_max_params(16000)
        assert 15 <= params <= 30


class TestRecommendedModel:
    def test_recommended_model_phone(self):
        """Adreno phone, 8GB RAM → Qwen3.5-2B."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(platform="android", gpu_type="adreno", ram_mb=8000, vram_mb=2048)
        rec = hw.recommended_model()
        assert rec is not None
        assert "2B" in rec[0]
        assert rec[1] == 2.8

    def test_recommended_model_imac_m1(self):
        """Apple Silicon 16GB → Qwen3.5-9B."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(platform="darwin", gpu_type="apple_silicon", ram_mb=16000, vram_mb=16000)
        rec = hw.recommended_model()
        assert rec is not None
        assert "9B" in rec[0]
        assert rec[1] == 6.0

    def test_recommended_model_midrange(self):
        """12GB RAM, no GPU → Qwen3.5-4B."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(gpu_type="none", ram_mb=12000, vram_mb=0)
        rec = hw.recommended_model()
        assert rec is not None
        assert "4B" in rec[0]

    def test_recommended_model_low_ram(self):
        """2GB RAM → None (can't run inference)."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(gpu_type="none", ram_mb=2000, vram_mb=0)
        rec = hw.recommended_model()
        assert rec is None

    def test_recommended_model_nvidia_16gb(self):
        """NVIDIA 16GB VRAM → Qwen3.5-9B."""
        from localforge.workers.detect import HardwareInfo
        hw = HardwareInfo(gpu_type="nvidia", ram_mb=32000, vram_mb=16000)
        rec = hw.recommended_model()
        assert rec is not None
        assert "9B" in rec[0]


class TestLlamaServerManager:
    def test_model_name_extraction(self):
        """LlamaServerManager.model_name extracts basename."""
        from localforge.workers.device_worker import LlamaServerManager
        mgr = LlamaServerManager(model_path="/mnt/models/Qwen3.5-2B-UD-Q8_K_XL.gguf")
        assert mgr.model_name == "Qwen3.5-2B-UD-Q8_K_XL.gguf"

    def test_model_name_nested_path(self):
        from localforge.workers.device_worker import LlamaServerManager
        mgr = LlamaServerManager(model_path="/home/user/.ai-hub-worker/models/test-model.gguf")
        assert mgr.model_name == "test-model.gguf"

    def test_default_port(self):
        from localforge.workers.device_worker import LlamaServerManager
        mgr = LlamaServerManager(model_path="/tmp/test.gguf")
        assert mgr.port == 5050
        assert mgr.url == "http://127.0.0.1:5050"

    def test_custom_port(self):
        from localforge.workers.device_worker import LlamaServerManager
        mgr = LlamaServerManager(model_path="/tmp/test.gguf", port=6060)
        assert mgr.port == 6060
        assert mgr.url == "http://127.0.0.1:6060"


class TestDeviceCapabilities:
    def test_from_dict_with_new_fields(self):
        """DeviceCapabilities.from_dict includes thermal/battery fields."""
        from localforge.gpu_pool import DeviceCapabilities
        caps = DeviceCapabilities.from_dict({
            "inference": True,
            "embeddings": True,
            "vram_mb": 4096,
            "battery_pct": 50,
            "battery_charging": False,
            "thermal_throttled": True,
            "gpu_type": "amd",
        })
        assert caps.inference is True
        assert caps.battery_pct == 50
        assert caps.thermal_throttled is True
        assert caps.gpu_type == "amd"

    def test_from_dict_ignores_unknown_fields(self):
        from localforge.gpu_pool import DeviceCapabilities
        caps = DeviceCapabilities.from_dict({
            "inference": True,
            "unknown_field": "value",
        })
        assert caps.inference is True
        assert not hasattr(caps, "unknown_field")
