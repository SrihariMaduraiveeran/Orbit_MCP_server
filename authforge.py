"""AuthForge OAuth 2.0 (client credentials) authentication as a pure ASGI middleware.

Callers obtain a short-lived access token from AuthForge (POST /api/oauth/token with their
client_id/client_secret) and send it on every request as `Authorization: Bearer at_live_...`.
Each token is checked with AuthForge's introspection endpoint (RFC 7662):
  active      -> request passes through with scope/app_id/client_id in request.state.authforge
  inactive    -> 401 with AuthForge's reason
  unreachable -> 503 (fail closed)
"""

import json
import logging
import os
import time
from typing import Optional

import httpx2
from starlette.types import ASGIApp, Receive, Scope, Send

from app_logging import client_info, get_logger, log, mask_key

logger = get_logger("auth")

TOKEN_PREFIX = "at_live_"

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
        self.introspect_url = f"{base_url.rstrip('/')}/api/oauth/introspect"
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

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request = {"method": scope["method"], "path": scope["path"],
                   **client_info(headers, (scope.get("client") or (None,))[0])}

        token = self._bearer_token(scope)
        if token is None:
            raw = headers.get("authorization")
            detail = "no Authorization header" if raw is None else f"not a Bearer token (scheme '{raw.split(' ')[0]}')"
            log(logger, logging.WARNING, "auth denied", status=401, reason="missing_token", detail=detail, **request)
            await self._reject(send, 401, "missing_token",
                               "Send 'Authorization: Bearer at_live_...' (get a token from AuthForge /api/oauth/token).")
            return
        if not token.startswith(TOKEN_PREFIX):
            # Not an AuthForge access token: reject without spending an introspect call
            log(logger, logging.WARNING, "auth denied", status=401, reason="invalid_token_format",
                token=mask_key(token), **request)
            await self._reject(send, 401, "invalid_token_format", f"Access tokens must start with {TOKEN_PREFIX}.")
            return

        start = time.perf_counter()
        result = await self._introspect(token)
        introspect_ms = round((time.perf_counter() - start) * 1000)
        if result is None:
            log(logger, logging.ERROR, "auth unavailable (fail closed)", status=503, reason="auth_unavailable",
                token=mask_key(token), introspect_ms=introspect_ms, **request)
            await self._reject(send, 503, "auth_unavailable", "Authentication service is unreachable. Try again later.")
            return
        if result.get("active") is not True:
            reason = result.get("reason") or "inactive"
            log(logger, logging.WARNING, "auth denied", status=401, reason=reason, token=mask_key(token),
                introspect_ms=introspect_ms, **request)
            await self._reject(send, 401, reason, f"Access token rejected: {reason}.")
            return

        auth = {
            "app_id": result.get("app_id"),
            "client_id": result.get("client_id"),
            "client_name": result.get("client_name"),
            "scope": result.get("scope"),
        }
        log(logger, logging.INFO, "auth ok", token=mask_key(token), introspect_ms=introspect_ms, **auth, **request)
        scope.setdefault("state", {})["authforge"] = auth
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

    async def _introspect(self, token: str) -> Optional[dict]:
        """AuthForge's introspection result, or None if it could not be obtained (-> fail closed)."""
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self.timeout)
        try:
            resp = await self._client.post(self.introspect_url, json={"token": token})
        except httpx2.HTTPError as e:
            logger.error("AuthForge unreachable at %s: %s", self.introspect_url, e)
            return None
        if resp.status_code >= 500:
            logger.error("AuthForge introspect returned HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.error("AuthForge introspect returned non-JSON response (HTTP %s)", resp.status_code)
            return None
        if not isinstance(data, dict) or "active" not in data:
            logger.error("AuthForge introspect returned an unexpected body (HTTP %s)", resp.status_code)
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
