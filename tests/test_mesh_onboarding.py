"""End-to-end tests for the mesh onboarding HTTP surface.

Spins up a Starlette app that mounts only the dashboard routes + auth
middleware, so we don't have to bring up the full MCP gateway. Drives the
full admin-mints-token → worker-registers → worker-heartbeats flow.
"""
from __future__ import annotations

import os

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from localforge.auth import BearerAuthMiddleware
from localforge.dashboard.routes import dashboard_routes


@pytest.fixture
def admin_key(monkeypatch):
    """Seed a known admin key via LOCAL_AI_KEY."""
    key = "test-admin-key-do-not-use-in-prod"
    monkeypatch.setenv("LOCAL_AI_KEY", key)
    return key


@pytest.fixture
def app(tmp_data_dir, admin_key):
    """Starlette app with just /api mounted + bearer middleware."""
    application = Starlette(routes=[Mount("/api", routes=dashboard_routes)])
    application.add_middleware(BearerAuthMiddleware)
    # Fresh in-memory enrollment store + empty registry for each test
    from localforge import enrollment
    enrollment._enrollment_store = enrollment.EnrollmentStore()
    enrollment._worker_registry = enrollment.WorkerRegistry(path=tmp_data_dir / "workers.json")
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestEnrollmentHTTP:
    def test_admin_mints_token_and_gets_install_commands(self, client, admin_key):
        resp = client.post("/api/mesh/enrollment-token",
                           headers=_auth(admin_key),
                           json={"note": "first laptop"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["token"]
        assert data["issued_by"] == "admin"
        cmds = data["install_commands"]
        assert set(cmds) == {"linux", "darwin", "win32", "android"}
        assert data["token"] in cmds["linux"]
        assert "/api/mesh/install-script" in cmds["linux"]

    def test_non_admin_cannot_mint_token(self, client):
        # No auth header → anonymous → 401 from middleware
        resp = client.post("/api/mesh/enrollment-token", json={})
        assert resp.status_code == 401

    def test_install_script_requires_valid_token(self, client):
        resp = client.get("/api/mesh/install-script?platform=linux&token=bogus")
        assert resp.status_code == 401

    def test_install_script_rejects_unknown_platform(self, client, admin_key):
        mint = client.post("/api/mesh/enrollment-token",
                           headers=_auth(admin_key), json={}).json()
        resp = client.get(f"/api/mesh/install-script?platform=bsd&token={mint['token']}")
        assert resp.status_code == 400

    def test_install_script_returns_linux_bootstrapper(self, client, admin_key):
        mint = client.post("/api/mesh/enrollment-token",
                           headers=_auth(admin_key), json={}).json()
        resp = client.get(f"/api/mesh/install-script?platform=linux&token={mint['token']}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/x-shellscript")
        # Must actually be the bash script we ship.
        assert b"#!/bin/bash" in resp.content
        assert b"LocalForge Linux Worker Setup" in resp.content

    @pytest.mark.parametrize("platform,magic", [
        ("linux",   b"#!/bin/bash"),
        ("darwin",  b"#!/bin/bash"),
        ("android", b"#!/data/data/com.termux/files/usr/bin/bash"),
        ("win32",   b"LocalForge Windows Worker Bootstrap"),
    ])
    def test_install_script_ships_all_four_platforms(self, client, admin_key, platform, magic):
        mint = client.post("/api/mesh/enrollment-token",
                           headers=_auth(admin_key), json={}).json()
        resp = client.get(f"/api/mesh/install-script?platform={platform}&token={mint['token']}")
        assert resp.status_code == 200, resp.text
        assert magic in resp.content


class TestRegisterAndHeartbeat:
    def test_register_requires_enrollment_token(self, client):
        resp = client.post("/api/mesh/register", json={"hostname": "x", "platform": "linux"})
        assert resp.status_code in (400, 401)

    def test_register_rejects_expired_token(self, client):
        resp = client.post("/api/mesh/register", json={
            "enrollment_token": "bogus",
            "hostname": "x",
            "platform": "linux",
            "hardware": {},
        })
        assert resp.status_code == 401

    def test_register_full_flow(self, client, admin_key):
        # 1. Admin mints token
        mint = client.post("/api/mesh/enrollment-token",
                           headers=_auth(admin_key), json={"note": "test-host"}).json()
        token = mint["token"]

        # 2. Worker registers with enrollment token (no bearer — public endpoint)
        reg = client.post("/api/mesh/register", json={
            "enrollment_token": token,
            "hostname": "test-host",
            "platform": "linux",
            "hardware": {"ram_mb": 16000, "tier": "cpu-capable"},
        })
        assert reg.status_code == 200, reg.text
        reg_data = reg.json()
        assert reg_data["status"] == "registered"
        assert reg_data["scopes"] == ["mesh"]
        worker_key = reg_data["api_key"]
        worker_id = reg_data["worker_id"]
        assert worker_key and worker_id.startswith("test-host-")

        # 3. Enrollment token is burned — reusing it fails
        reuse = client.post("/api/mesh/register", json={
            "enrollment_token": token,
            "hostname": "other",
            "platform": "linux",
            "hardware": {},
        })
        assert reuse.status_code == 401

        # 4. Worker key authenticates /api/mesh/heartbeat (scoped mesh)
        hb = client.post("/api/mesh/heartbeat",
                        headers=_auth(worker_key),
                        json={"hostname": "test-host"})
        # Gateway's gpu_pool_ref is None in this test harness, so we expect 503
        # but NOT 401 — auth should have passed and scope check should have passed.
        assert hb.status_code != 401, "worker key must authenticate"
        assert hb.status_code != 403, "worker key must have mesh scope"

        # 5. Worker key CANNOT hit an admin-only endpoint
        forbidden = client.post("/api/mesh/enrollment-token",
                                headers=_auth(worker_key), json={})
        assert forbidden.status_code == 403

    def test_admin_can_list_and_revoke_worker(self, client, admin_key):
        mint = client.post("/api/mesh/enrollment-token",
                           headers=_auth(admin_key), json={}).json()
        reg = client.post("/api/mesh/register", json={
            "enrollment_token": mint["token"],
            "hostname": "doomed",
            "platform": "linux",
            "hardware": {},
        }).json()

        listed = client.get("/api/mesh/workers", headers=_auth(admin_key)).json()
        assert any(w["worker_id"] == reg["worker_id"] for w in listed["workers"])
        # list_workers must never leak the bcrypt hash
        assert all("api_key_hash" not in w for w in listed["workers"])

        rev = client.post("/api/mesh/workers/revoke",
                          headers=_auth(admin_key),
                          json={"worker_id": reg["worker_id"]})
        assert rev.status_code == 200

        # Revoked worker key no longer authenticates
        hb = client.post("/api/mesh/heartbeat",
                        headers=_auth(reg["api_key"]),
                        json={"hostname": "doomed"})
        assert hb.status_code == 401


class TestScopeEnforcement:
    def test_admin_scopes_allow_everything(self, client, admin_key):
        resp = client.get("/api/mesh/workers", headers=_auth(admin_key))
        assert resp.status_code == 200

    def test_anonymous_blocked_at_middleware(self, client):
        resp = client.get("/api/mesh/workers")
        assert resp.status_code == 401
