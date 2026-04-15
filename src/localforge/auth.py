"""Bearer-token auth middleware for the MCP HTTP gateway.

Supports:
  - Bcrypt-hashed API keys (keys starting with $2b$ are compared via bcrypt)
  - Plaintext API keys (for dev/testing — migrate to hashed for production)
  - Environment variable keys: LOCAL_AI_KEY (primary), LOCAL_AI_KEY_OLD (rotation)
  - Multi-user profiles: each API key maps to a user with name and role
  - Token-bucket rate limiting per IP address
  - Key rotation: set LOCAL_AI_KEY_OLD to keep accepting the previous key
  - User profile stored on request.state.user for downstream routes

To generate a hashed key:
    python3 -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_KEY', bcrypt.gensalt()).decode())"

Key resolution order:
  1. Environment vars: LOCAL_AI_KEY, LOCAL_AI_KEY_OLD (rotation)
  2. config.yaml: gateway.api_keys list
  3. config.yaml: users.*.api_key per-user keys
"""

import hmac
import logging
import os
import time

import yaml
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from localforge.config import load_config_cached as _load_config
from localforge.paths import config_path

log = logging.getLogger("localforge.auth")

# Paths that don't require auth
# /api/mesh/register and /api/mesh/install-script are public because they
# authenticate via a short-lived enrollment token (validated inside the handler),
# not via a bearer API key.
PUBLIC_PATHS = {
    "/health",
    "/",
    "",
    "/api/mesh/register",
    "/api/mesh/install-script",
}

# ---------------------------------------------------------------------------
# Rate limiter — token bucket per IP
# ---------------------------------------------------------------------------
_rate_buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_refill_time)
RATE_LIMIT = 60       # requests per window
RATE_WINDOW = 60.0    # window in seconds
RATE_BURST = 20       # max burst above steady rate


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.monotonic()
    tokens, last_refill = _rate_buckets.get(ip, (RATE_LIMIT + RATE_BURST, now))

    # Refill tokens based on elapsed time
    elapsed = now - last_refill
    tokens = min(RATE_LIMIT + RATE_BURST, tokens + elapsed * (RATE_LIMIT / RATE_WINDOW))

    if tokens < 1:
        return False

    _rate_buckets[ip] = (tokens - 1, now)

    # Periodic cleanup: remove stale entries (threshold lowered to prevent memory leak)
    if len(_rate_buckets) > 100:
        cutoff = now - RATE_WINDOW * 2
        stale = [k for k, (_, t) in _rate_buckets.items() if t < cutoff]
        for k in stale:
            del _rate_buckets[k]

    return True


# ---------------------------------------------------------------------------
# Key comparison
# ---------------------------------------------------------------------------


def _check_key(provided: str, stored: str) -> bool:
    """Compare a provided key against a stored key.

    Stored key may be:
      - Bcrypt hash ($2b$...) — compared via bcrypt.checkpw
      - Plaintext — direct comparison
    """
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        try:
            import bcrypt
            return bcrypt.checkpw(provided.encode(), stored.encode())
        except ImportError:
            log.warning("bcrypt not installed — cannot verify hashed key")
            return False
        except (ValueError, TypeError) as e:
            log.warning("bcrypt verification failed: %s", e)
            return False
    return hmac.compare_digest(provided, stored)


def _load_and_check_key(token: str) -> str | None:
    """Check token against all configured keys. Returns the raw config key that matched, or None.

    Resolution order:
      1. LOCAL_AI_KEY env var (primary)
      2. LOCAL_AI_KEY_OLD env var (rotation — accepts old key during transition)
      3. gateway.api_keys from config.yaml
      4. users.*.api_key from config.yaml
      5. Worker registry (workers.json — bcrypt-hashed, scoped to mesh)
    """
    # 1-2. Environment variable keys (fast path, no config parse)
    for env_name in ("LOCAL_AI_KEY", "LOCAL_AI_KEY_OLD"):
        env_key = os.environ.get(env_name, "")
        if env_key and _check_key(token, env_key):
            return env_key

    cfg = _load_config()

    # 3. Gateway API keys
    for key in cfg.get("gateway", {}).get("api_keys", []):
        if key and _check_key(token, key):
            return key

    # 4. User-specific keys
    for user_cfg in cfg.get("users", {}).values():
        key = user_cfg.get("api_key", "")
        if key and _check_key(token, key):
            return key

    # 5. Worker registry — long-lived, scoped mesh-only keys
    try:
        from localforge.enrollment import worker_registry
        if worker_registry().find_by_key(token) is not None:
            return token  # sentinel: worker key lookup handled by _resolve_user
    except Exception as e:  # pragma: no cover — don't let optional lookup break auth
        log.debug("worker registry lookup failed: %s", e)

    return None


def _resolve_user(token: str) -> dict:
    """Resolve an API key to a user profile.

    Users get full scope implicitly; workers get an explicit ``scopes`` list
    restricting them to mesh endpoints.
    """
    cfg = _load_config()

    # Check users section first (has richest profile)
    for user_id, user_cfg in cfg.get("users", {}).items():
        stored_key = user_cfg.get("api_key", "")
        if stored_key and _check_key(token, stored_key):
            return {
                "id": user_id,
                "name": user_cfg.get("name", user_id),
                "role": user_cfg.get("role", "user"),
                "scopes": ["*"],
            }

    # Env var keys → admin
    for env_name in ("LOCAL_AI_KEY", "LOCAL_AI_KEY_OLD"):
        env_key = os.environ.get(env_name, "")
        if env_key and _check_key(token, env_key):
            return {"id": "admin", "name": "Admin (env)", "role": "admin", "scopes": ["*"]}

    # gateway.api_keys match → default admin user
    for key in cfg.get("gateway", {}).get("api_keys", []):
        if key and _check_key(token, key):
            return {"id": "admin", "name": "Admin", "role": "admin", "scopes": ["*"]}

    # Worker registry — scoped to mesh
    try:
        from localforge.enrollment import worker_registry
        record = worker_registry().find_by_key(token)
        if record is not None:
            return {
                "id": record["worker_id"],
                "name": record.get("hostname", record["worker_id"]),
                "role": record.get("role", "worker"),
                "scopes": record.get("scopes", ["mesh"]),
                "platform": record.get("platform", "unknown"),
            }
    except Exception as e:  # pragma: no cover
        log.debug("worker registry resolve failed: %s", e)

    return {"id": "anonymous", "name": "Anonymous", "role": "user", "scopes": []}


def require_scope(request: Request, scope: str) -> JSONResponse | None:
    """Return a 403 JSONResponse if ``request.state.user`` lacks the given scope,
    or ``None`` if the check passes. Handlers use it like::

        denied = require_scope(request, "mesh")
        if denied:
            return denied
    """
    user = getattr(request.state, "user", None) or {}
    scopes = user.get("scopes", [])
    if "*" in scopes or scope in scopes:
        return None
    return JSONResponse(
        {"error": f"Missing required scope: {scope}", "user": user.get("id", "anonymous")},
        status_code=403,
    )


def require_role(request: Request, role: str) -> JSONResponse | None:
    """Return a 403 if the authenticated user doesn't match ``role``."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == role:
        return None
    return JSONResponse(
        {"error": f"Requires role: {role}", "user": user.get("id", "anonymous")},
        status_code=403,
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/")

        # Public endpoints — no auth required
        if path in PUBLIC_PATHS or path.startswith("/static"):
            request.state.user = {"id": "anonymous", "name": "Guest", "role": "guest"}
            return await call_next(request)

        # Rate limiting
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            return JSONResponse(
                {"error": "Rate limit exceeded. Try again shortly."},
                status_code=429,
            )

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
        if not token:
            return JSONResponse(
                {"error": "Empty bearer token"},
                status_code=401,
            )

        matched_key = _load_and_check_key(token)

        if matched_key is None:
            cfg = _load_config()
            has_keys = (
                bool(os.environ.get("LOCAL_AI_KEY"))
                or bool(cfg.get("gateway", {}).get("api_keys"))
                or bool(cfg.get("users"))
            )
            if not has_keys:
                return JSONResponse(
                    {"error": "No API keys configured. Set LOCAL_AI_KEY or config.yaml gateway.api_keys"},
                    status_code=500,
                )
            log.warning("Failed auth attempt from %s (path: %s)", client_ip, path)
            return JSONResponse({"error": "Invalid API key"}, status_code=401)

        # Resolve user profile and store on request state
        request.state.user = _resolve_user(token)
        return await call_next(request)
