"""Bearer-token auth middleware for the MCP HTTP gateway.

Supports multi-user profiles: each API key maps to a user with a name and role.
User profile is stored on request.state.user for downstream routes.
"""

import yaml
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Paths that don't require auth
PUBLIC_PATHS = {"/health", "/", ""}


def _load_config() -> dict:
    """Load config from config.yaml (re-read on each call for hot-reload)."""
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_api_keys() -> list[str]:
    """Load API keys from config.yaml."""
    cfg = _load_config()
    # Support both old format (gateway.api_keys list) and new (users section)
    keys = list(cfg.get("gateway", {}).get("api_keys", []))
    for user in cfg.get("users", {}).values():
        key = user.get("api_key", "")
        if key and key not in keys:
            keys.append(key)
    return keys


def _resolve_user(token: str) -> dict:
    """Resolve an API key to a user profile."""
    cfg = _load_config()

    # Check users section first
    for user_id, user_cfg in cfg.get("users", {}).items():
        if user_cfg.get("api_key") == token:
            return {
                "id": user_id,
                "name": user_cfg.get("name", user_id),
                "role": user_cfg.get("role", "user"),
            }

    # Fallback: gateway.api_keys → default admin user
    gateway_keys = cfg.get("gateway", {}).get("api_keys", [])
    if token in gateway_keys:
        return {"id": "admin", "name": "Admin", "role": "admin"}

    return {"id": "anonymous", "name": "Anonymous", "role": "user"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/")

        # Public endpoints — no auth required
        if path in PUBLIC_PATHS or path.startswith("/static"):
            request.state.user = {"id": "anonymous", "name": "Guest", "role": "guest"}
            return await call_next(request)

        # Extract Bearer token (header or query param for SSE/EventSource)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            # Fallback: check query param (for EventSource which can't set headers)
            token_param = request.query_params.get("token", "")
            if token_param:
                auth_header = f"Bearer {token_param}"
            else:
                return JSONResponse(
                    {"error": "Missing or malformed Authorization header. Use: Bearer <key>"},
                    status_code=401,
                )

        token = auth_header[7:]  # strip "Bearer "
        valid_keys = _load_api_keys()

        if not valid_keys:
            return JSONResponse(
                {"error": "No API keys configured in config.yaml gateway.api_keys"},
                status_code=500,
            )

        if token not in valid_keys:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)

        # Resolve user profile and store on request state
        request.state.user = _resolve_user(token)
        return await call_next(request)
