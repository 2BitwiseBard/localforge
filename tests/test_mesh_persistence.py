"""Tests for mesh heartbeat registry persistence to SQLite."""

import time

from localforge.gpu_pool import GPUPool


class TestMeshPersistence:
    def test_register_heartbeat_basic(self):
        pool = GPUPool({})
        key, accepted = pool.register_heartbeat({
            "hostname": "worker-1",
            "port": 8200,
            "tier": "gpu",
            "model_name": "test-model",
        })
        assert accepted
        assert key == "worker-1:8200"
        workers = pool.get_mesh_workers()
        assert len(workers) == 1
        assert workers[0]["hostname"] == "worker-1"

    def test_stale_cleanup_on_register(self):
        pool = GPUPool({})
        # Insert a stale node manually
        pool._heartbeat_nodes["stale:8200"] = {
            "hostname": "stale",
            "port": 8200,
            "tier": "cpu",
            "capabilities": {},
            "model_name": "",
            "active_tasks": 0,
            "stats": {},
            "uptime_s": 0,
            "last_heartbeat": time.time() - 700,  # >10 min ago
        }
        assert len(pool._heartbeat_nodes) == 1

        # Register a new node — should clean up the stale one
        pool.register_heartbeat({"hostname": "fresh", "port": 8200})
        assert "stale:8200" not in pool._heartbeat_nodes
        assert "fresh:8200" in pool._heartbeat_nodes

    def test_persist_and_load(self, tmp_path, monkeypatch):
        """Nodes should survive a pool restart via SQLite persistence."""
        # Patch data_dir to use tmp_path
        monkeypatch.setattr("localforge.gpu_pool.GPUPool._mesh_db_path", lambda self: tmp_path / "mesh.db")

        pool1 = GPUPool({})
        pool1.register_heartbeat({
            "hostname": "persistent-worker",
            "port": 8200,
            "tier": "gpu",
            "model_name": "llama-7b",
        })

        # Create a new pool (simulating restart) and load persisted nodes
        pool2 = GPUPool({})
        monkeypatch.setattr("localforge.gpu_pool.GPUPool._mesh_db_path", lambda self: tmp_path / "mesh.db")
        pool2._load_persisted_nodes()

        assert "persistent-worker:8200" in pool2._heartbeat_nodes
        node = pool2._heartbeat_nodes["persistent-worker:8200"]
        assert node["hostname"] == "persistent-worker"
        assert node["model_name"] == "llama-7b"
        assert node["tier"] == "gpu"

    def test_capacity_limit(self):
        pool = GPUPool({})
        pool._max_heartbeat_nodes = 3

        for i in range(5):
            key, accepted = pool.register_heartbeat({"hostname": f"w{i}", "port": 8200})
            if i < 3:
                assert accepted, f"Worker {i} should be accepted"
            else:
                assert not accepted, f"Worker {i} should be rejected (at capacity)"

        assert len(pool._heartbeat_nodes) == 3
