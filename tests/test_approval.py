"""Tests for the agent approval queue."""


from localforge.agents.approval import APPROVAL_REQUIRED, ApprovalQueue


class TestApprovalQueue:
    def setup_method(self):
        # Use in-memory SQLite — let _get_conn() handle schema creation
        self.aq = ApprovalQueue(db_path=":memory:")
        self.aq._get_conn()

    def test_request_and_list(self):
        req_id = self.aq.request_approval("agent-1", "swap_model", {"model_name": "test"})
        assert req_id
        pending = self.aq.list_pending()
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "swap_model"
        assert pending[0]["agent_id"] == "agent-1"

    def test_approve(self):
        req_id = self.aq.request_approval("agent-1", "unload_model", {})
        assert self.aq.approve(req_id, decided_by="tyler")
        pending = self.aq.list_pending()
        assert len(pending) == 0
        recent = self.aq.list_recent()
        assert len(recent) == 1
        assert recent[0]["status"] == "approved"
        assert recent[0]["decided_by"] == "tyler"

    def test_deny(self):
        req_id = self.aq.request_approval("agent-1", "delete_index", {"name": "test"})
        assert self.aq.deny(req_id)
        pending = self.aq.list_pending()
        assert len(pending) == 0
        recent = self.aq.list_recent()
        assert recent[0]["status"] == "denied"

    def test_double_decide_fails(self):
        req_id = self.aq.request_approval("agent-1", "swap_model", {})
        self.aq.approve(req_id)
        # Second decision should fail (already decided)
        assert not self.aq.deny(req_id)

    def test_needs_approval(self):
        assert self.aq.needs_approval("swap_model")
        assert self.aq.needs_approval("unload_model")
        assert self.aq.needs_approval("delete_index")
        assert not self.aq.needs_approval("local_chat")
        assert not self.aq.needs_approval("health_check")
        assert not self.aq.needs_approval("analyze_code")

    def test_approval_required_set(self):
        """Verify the APPROVAL_REQUIRED set has sensible entries."""
        assert "swap_model" in APPROVAL_REQUIRED
        assert "unload_model" in APPROVAL_REQUIRED
        assert "delete_index" in APPROVAL_REQUIRED
        assert "local_chat" not in APPROVAL_REQUIRED

    def test_multiple_pending(self):
        id1 = self.aq.request_approval("agent-1", "swap_model", {})
        id2 = self.aq.request_approval("agent-2", "delete_note", {"topic": "test"})
        pending = self.aq.list_pending()
        assert len(pending) == 2
        self.aq.approve(id1)
        pending = self.aq.list_pending()
        assert len(pending) == 1
        assert pending[0]["id"] == id2

    def test_priority_levels(self):
        self.aq.request_approval("agent-1", "swap_model", {}, priority="normal")
        self.aq.request_approval("agent-2", "unload_model", {}, priority="urgent")
        pending = self.aq.list_pending()
        assert len(pending) == 2
        # Urgent should come first
        assert pending[0]["priority"] == "urgent"
        assert pending[1]["priority"] == "normal"

    def test_urgent_default_ttl(self):
        self.aq.request_approval("agent-1", "swap_model", {}, priority="urgent")
        pending = self.aq.list_pending()
        assert pending[0]["ttl_seconds"] == 120  # urgent default

    def test_audit_log(self):
        req_id = self.aq.request_approval("agent-1", "swap_model", {"model": "test"})
        self.aq.approve(req_id, decided_by="tyler")
        audit = self.aq.get_audit_log(request_id=req_id)
        assert len(audit) == 2  # requested + approved
        assert audit[0]["action"] == "requested"
        assert audit[1]["action"] == "approved"
        assert audit[1]["decided_by"] == "tyler"

    def test_audit_log_deny(self):
        req_id = self.aq.request_approval("agent-1", "delete_index", {})
        self.aq.deny(req_id, decided_by="system")
        audit = self.aq.get_audit_log(request_id=req_id)
        assert any(a["action"] == "denied" for a in audit)

    def test_audit_log_global(self):
        self.aq.request_approval("a1", "swap_model", {})
        self.aq.request_approval("a2", "unload_model", {})
        audit = self.aq.get_audit_log()
        assert len(audit) == 2  # 2 "requested" entries
