"""AuthForge API-key authentication as a pure ASGI middleware.

Every HTTP request (except public paths) must carry `Authorization: Bearer sk_live_...`.
The key is checked against AuthForge's verify endpoint on each request:
  valid       -> request passes through with scope/app_id in request.state.authforge
  invalid     -> 401 with AuthForge's reason
  unreachable -> 503 (fail closed)
"""

import json
import logging
import os
from typing import Optional

import httpx2
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("authforge")

KEY_PREFIX = "sk_live_"

# /download/enriched is public because export links are opened in a browser, which cannot send a
# bearer header; those links carry their own signed, expiring token instead.
DEFAULT_PUBLIC_PATHS = "/health,/docs,/openapi.json,/download/enriched"


def authforge_enabled() -> bool:
    return os.getenv("AUTHFORGE_DISABLED", "").lower() != "true"


class AuthForgeMiddleware:
    def __init__(self, app: ASGIApp, base_url: Optional[str] = None, public_paths: Optional[list[str]] = None,
                 timeout: Optional[float] = None):
        self.app = app
        base_url = base_url or os.getenv("AUTHFORGE_URL")
        if not base_url:
            # Fail closed: without AuthForge the server must not start unauthenticated
            raise RuntimeError("AUTHFORGE_URL is not set. Set it, or AUTHFORGE_DISABLED=true for local development.")
        base_url = base_url.rstrip("/")
        self.verify_url = f"{base_url}/api/auth/verify"
        paths = public_paths if public_paths is not None else \
            os.getenv("AUTHFORGE_PUBLIC_PATHS", DEFAULT_PUBLIC_PATHS).split(",")
        self.public_paths = [p.strip().rstrip("/") for p in paths if p.strip()]
        self.timeout = timeout if timeout is not None else float(os.getenv("AUTHFORGE_TIMEOUT_SECONDS", "5"))
        self._client: Optional[httpx2.AsyncClient] = None

    def _is_public(self, path: str) -> bool:
        path = path.rstrip("/") or "/"
        return any(path == p or path.startswith(p + "/") for p in self.public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_public(scope["path"]):
            await self.app(scope, receive, send)
            return

        api_key = self._bearer_token(scope)
        if api_key is None:
            await self._reject(send, 401, "missing_api_key", "Send 'Authorization: Bearer sk_live_...'.")
            return
        if not api_key.startswith(KEY_PREFIX):
            # Not an AuthForge key: reject without spending a verify call
            await self._reject(send, 401, "invalid_key_format", f"API keys must start with {KEY_PREFIX}.")
            return

        result = await self._verify(api_key)
        if result is None:
            await self._reject(send, 503, "auth_unavailable", "Authentication service is unreachable. Try again later.")
            return
        if not result.get("valid"):
            reason = result.get("reason") or "invalid"
            await self._reject(send, 401, reason, f"API key rejected: {reason}.")
            return

        scope.setdefault("state", {})["authforge"] = {
            "app_id": result.get("app_id"),
            "key_name": result.get("key_name"),
            "scope": result.get("scope"),
        }
        await self.app(scope, receive, send)

    @staticmethod
    def _bearer_token(scope: Scope) -> Optional[str]:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, token = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and token.strip():
                    return token.strip()
                return None
        return None

    async def _verify(self, api_key: str) -> Optional[dict]:
        """AuthForge's verdict, or None if it could not be obtained (-> fail closed)."""
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self.timeout)
        try:
            resp = await self._client.post(self.verify_url, json={"api_key": api_key})
        except httpx2.HTTPError as e:
            logger.error("AuthForge unreachable at %s: %s", self.verify_url, e)
            return None
        if resp.status_code >= 500:
            logger.error("AuthForge returned HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.error("AuthForge returned non-JSON response (HTTP %s)", resp.status_code)
            return None
        if not isinstance(data, dict) or "valid" not in data:
            logger.error("AuthForge returned an unexpected body (HTTP %s)", resp.status_code)
            return None
        return data

    @staticmethod
    async def _reject(send: Send, status: int, reason: str, message: str) -> None:
        body = json.dumps({"error": "unauthorized" if status == 401 else "service_unavailable",
                           "reason": reason, "message": message}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if status == 401:
            headers.append((b"www-authenticate", f'Bearer error="invalid_token", error_description="{reason}"'.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
