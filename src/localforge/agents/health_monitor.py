"""Health monitor agent: pings all services, logs alerts on failure."""

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class HealthMonitor(BaseAgent):
    name = "health-monitor"
    trust_level = TrustLevel.MONITOR
    description = "Pings all services and logs alerts on failure"

    async def run(self):
        self.state.log("Running health check")

        result = await self.call_tool("health_check", {})
        text = self.extract_text(result)

        # Check for real issues — ignore optional backends being unreachable
        lines = text.splitlines()
        has_error = "error" in text.lower()
        has_primary_down = any(
            "unreachable" in line.lower() and "optional" not in line.lower()
            for line in lines
        )

        if has_error or has_primary_down:
            self.state.log(f"ALERT: Health issue detected:\n{text}")
            await self.notify(
                "Service health issue detected",
                text[:500],
                level="critical" if has_primary_down else "warning",
            )
        else:
            self.state.log("All services healthy")

        # Track in state
        self.state.data["last_health"] = text[:200]
