"""Tests for mesh improvements: routing logging, compute_test tool, dispatch retry."""


from localforge.gpu_pool import Backend, CircuitState, GPUPool


class TestGPUPoolRecordMethods:
    def test_record_failure_updates_circuit(self):
        pool = GPUPool({})
        pool._backends = {
            "local": Backend(
                name="local",
                url="http://localhost:5000/v1",
            )
        }
        # Record enough failures to trip the circuit breaker
        for _ in range(5):
            pool.record_failure("http://localhost:5000/v1")
        assert pool._backends["local"].circuit.state == CircuitState.OPEN

    def test_record_success_resets_circuit(self):
        pool = GPUPool({})
        pool._backends = {
            "local": Backend(
                name="local",
                url="http://localhost:5000/v1",
            )
        }
        # Trip the breaker
        for _ in range(5):
            pool.record_failure("http://localhost:5000/v1")
        assert pool._backends["local"].circuit.state == CircuitState.OPEN

        # Reset via success (need to wait for half-open or force it)
        pool._backends["local"].circuit.state = CircuitState.HALF_OPEN
        pool.record_success("http://localhost:5000/v1")
        assert pool._backends["local"].circuit.state == CircuitState.CLOSED

    def test_record_failure_unknown_url_noop(self):
        """Recording failure for unknown URL doesn't crash."""
        pool = GPUPool({})
        pool.record_failure("http://unknown:9999/v1")  # should not raise

    def test_record_success_unknown_url_noop(self):
        """Recording success for unknown URL doesn't crash."""
        pool = GPUPool({})
        pool.record_success("http://unknown:9999/v1")  # should not raise


class TestRouteRequestLogging:
    def test_route_request_returns_url(self):
        """route_request returns a URL when backends are available."""
        pool = GPUPool({})
        pool._backends = {
            "local": Backend(
                name="local",
                url="http://localhost:5000/v1",
                healthy=True,
            )
        }
        result = pool.route_request("default")
        assert result == "http://localhost:5000/v1"

    def test_route_request_returns_none_when_empty(self):
        """route_request returns None when no backends available."""
        pool = GPUPool({})
        result = pool.route_request("default")
        assert result is None


class TestComputeTestToolRegistered:
    def test_compute_test_in_registry(self):
        """compute_test tool is registered."""
        from localforge.tools import _tool_handlers
        assert "compute_test" in _tool_handlers
