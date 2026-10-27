"""AuthForge OAuth 2.0 (client credentials) authentication as a pure ASGI middleware.

Callers obtain a short-lived access token from AuthForge (POST /api/oauth/token with their
client_id/client_secret) and send it on every request as `Authorization: Bearer at_live_...`.
Each token is checked with AuthForge's introspection endpoint (RFC 7662):
  active      -> request passes through with scope/app_id/client_id in request.state.authforge
  inactive    -> 401 with AuthForge's reason
  unreachable -> 503 (fail closed)

Clients that can only send a fixed header (e.g. Claude Desktop via mcp-remote) may instead send
`Authorization: Basic base64(client_id:client_secret)`. The server then obtains the at_live_ token
itself (cached until shortly before expiry) and introspects it like a Bearer token, so every request
is still validated and counted by AuthForge. Disable with AUTHFORGE_ALLOW_BASIC=false.

OAuth discovery for Claude's "Add custom connector": the server publishes RFC 9728 Protected Resource
Metadata at /.well-known/oauth-protected-resource (and /.well-known/oauth-protected-resource/mcp)
naming AuthForge as the authorization server, and every 401 carries
`WWW-Authenticate: Bearer resource_metadata="..."`, so the client can run Authorization Code + PKCE
against AuthForge and come back with an at_live_ token that is introspected like any other.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import httpx2
from starlette.types import ASGIApp, Receive, Scope, Send

from app_logging import client_info, get_logger, log, mask_key

logger = get_logger("auth")

TOKEN_PREFIX = "at_live_"

# /download/enriched is public because export links are opened in a browser, which cannot send a
# bearer header; those links carry their own signed, expiring token instead.
DEFAULT_PUBLIC_PATHS = "/health,/docs,/openapi.json,/download/enriched"


BASIC_FAILURE_CACHE_SECONDS = 10
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"


@dataclass
class _TokenDenied:
    reason: str


def authforge_enabled() -> bool:
    return os.getenv("AUTHFORGE_DISABLED", "").lower() != "true"


class AuthForgeMiddleware:
    def __init__(self, app: ASGIApp, base_url: Optional[str] = None, public_paths: Optional[list[str]] = None,
                 timeout: Optional[float] = None, mcp_public_url: Optional[str] = None):
        self.app = app
        base_url = base_url or os.getenv("AUTHFORGE_URL")
        if not base_url:
            # Fail closed: without AuthForge the server must not start unauthenticated
            raise RuntimeError("AUTHFORGE_URL is not set. Set it, or AUTHFORGE_DISABLED=true for local development.")
        self.authforge_url = base_url.rstrip("/")

        # RFC 9728 Protected Resource Metadata: tells OAuth clients (e.g. Claude's custom connector)
        # that tokens for this server come from AuthForge.
        mcp_public_url = (mcp_public_url or os.getenv("MCP_PUBLIC_URL") or "").rstrip("/")
        if not mcp_public_url:
            raise RuntimeError("MCP_PUBLIC_URL is not set (the public URL clients use, ending in /mcp).")
        parsed = urlsplit(mcp_public_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        self.resource_metadata_url = f"{origin}{RESOURCE_METADATA_PATH}"
        self.resource_metadata_paths = {RESOURCE_METADATA_PATH, RESOURCE_METADATA_PATH + (parsed.path.rstrip("/") or "")}
        self.resource_metadata = json.dumps({
            "resource": mcp_public_url,
            "authorization_servers": [self.authforge_url],
            "scopes_supported": ["read", "write", "full"],
            "bearer_methods_supported": ["header"],
        }).encode()
        self.introspect_url = f"{base_url.rstrip('/')}/api/oauth/introspect"
        self.token_url = f"{base_url.rstrip('/')}/api/oauth/token"
        # Basic client_id:client_secret is exchanged here for a token (for clients that can only send a fixed header)
        self.allow_basic = os.getenv("AUTHFORGE_ALLOW_BASIC", "true").lower() == "true"
        self._basic_tokens: dict[str, tuple[str, float]] = {}  # sha256(id:secret) -> (token, expires_at)
        self._basic_failures: dict[str, tuple[float, str]] = {}  # sha256(id:secret) -> (retry_after, reason)
        self._basic_locks: dict[str, asyncio.Lock] = {}
        paths = public_paths if public_paths is not None else \
            os.getenv("AUTHFORGE_PUBLIC_PATHS", DEFAULT_PUBLIC_PATHS).split(",")
        self.public_paths = [p.strip().rstrip("/") for p in paths if p.strip()]
        self.timeout = timeout if timeout is not None else float(os.getenv("AUTHFORGE_TIMEOUT_SECONDS", "5"))
        self._client: Optional[httpx2.AsyncClient] = None

    def _is_public(self, path: str) -> bool:
        path = path.rstrip("/") or "/"
        return any(path == p or path.startswith(p + "/") for p in self.public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") in self.resource_metadata_paths:
            await self._send_resource_metadata(scope, send)
            return
        if scope["type"] != "http" or self._is_public(scope["path"]):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request = {"method": scope["method"], "path": scope["path"],
                   **client_info(headers, (scope.get("client") or (None,))[0])}

        basic = self._basic_credentials(scope) if self.allow_basic else None
        if basic is not None:
            await self._handle_basic(basic, request, scope, receive, send)
            return

        token = self._bearer_token(scope)
        if token is None:
            raw = headers.get("authorization")
            detail = "no Authorization header" if raw is None else f"unsupported scheme '{raw.split(' ')[0]}'"
            log(logger, logging.WARNING, "auth denied", status=401, reason="missing_token", detail=detail, **request)
            await self._reject(send, 401, "missing_token",
                               "Send 'Authorization: Bearer at_live_...' (get a token from AuthForge /api/oauth/token)"
                               + (" or 'Authorization: Basic base64(client_id:client_secret)'." if self.allow_basic else "."))
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

    # --- Basic auth: client_id/client_secret exchanged server-side for an at_live_ token -------

    async def _handle_basic(self, creds: tuple[str, str], request: dict, scope: Scope,
                            receive: Receive, send: Send) -> None:
        client_id, client_secret = creds
        cache_key = hashlib.sha256(f"{client_id}:{client_secret}".encode()).hexdigest()
        request = {**request, "auth_method": "basic", "client_id": client_id}

        failed_until, failed_reason = self._basic_failures.get(cache_key, (0.0, ""))
        if time.time() < failed_until:
            # Same bad credentials again within a few seconds: don't hammer AuthForge
            log(logger, logging.WARNING, "auth denied", status=401, reason=failed_reason, cached=True, **request)
            await self._reject(send, 401, failed_reason, f"Client credentials rejected: {failed_reason}.")
            return

        start = time.perf_counter()
        for attempt in (1, 2):
            token = await self._token_for(cache_key, client_id, client_secret, force=attempt == 2)
            if token is None:
                log(logger, logging.ERROR, "auth unavailable (fail closed)", status=503, reason="auth_unavailable",
                    **request)
                await self._reject(send, 503, "auth_unavailable", "Authentication service is unreachable. Try again later.")
                return
            if isinstance(token, _TokenDenied):
                self._basic_failures[cache_key] = (time.time() + BASIC_FAILURE_CACHE_SECONDS, token.reason)
                log(logger, logging.WARNING, "auth denied", status=401, reason=token.reason, **request)
                await self._reject(send, 401, token.reason, f"Client credentials rejected: {token.reason}.")
                return

            result = await self._introspect(token)
            if result is None:
                log(logger, logging.ERROR, "auth unavailable (fail closed)", status=503, reason="auth_unavailable",
                    token=mask_key(token), **request)
                await self._reject(send, 503, "auth_unavailable", "Authentication service is unreachable. Try again later.")
                return
            if result.get("active") is True:
                break
            reason = result.get("reason") or "inactive"
            self._basic_tokens.pop(cache_key, None)
            if attempt == 1 and reason in ("revoked", "expired", "inactive"):
                continue  # cached token no longer valid: fetch a new one once
            log(logger, logging.WARNING, "auth denied", status=401, reason=reason, token=mask_key(token), **request)
            await self._reject(send, 401, reason, f"Access token rejected: {reason}.")
            return

        auth = {
            "app_id": result.get("app_id"),
            "client_id": result.get("client_id"),
            "client_name": result.get("client_name"),
            "scope": result.get("scope"),
        }
        log(logger, logging.INFO, "auth ok", token=mask_key(token),
            auth_ms=round((time.perf_counter() - start) * 1000), **auth,
            **{k: v for k, v in request.items() if k != "client_id"})
        scope.setdefault("state", {})["authforge"] = auth
        await self.app(scope, receive, send)

    async def _token_for(self, cache_key: str, client_id: str, client_secret: str, force: bool = False):
        """Cached at_live_ token for these credentials; _TokenDenied if AuthForge refuses; None if unreachable."""
        cached = self._basic_tokens.get(cache_key)
        if cached and not force and time.time() < cached[1] - 60:
            return cached[0]
        lock = self._basic_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._basic_tokens.get(cache_key)
            if cached and not force and time.time() < cached[1] - 60:
                return cached[0]  # another request refreshed it meanwhile
            if self._client is None:
                self._client = httpx2.AsyncClient(timeout=self.timeout)
            try:
                resp = await self._client.post(self.token_url, auth=(client_id, client_secret),
                                               data={"grant_type": "client_credentials"})
            except httpx2.HTTPError as e:
                logger.error("AuthForge unreachable at %s: %s", self.token_url, e)
                return None
            if resp.status_code >= 500:
                logger.error("AuthForge token endpoint returned HTTP %s", resp.status_code)
                return None
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code != 200 or "access_token" not in data:
                detail = data.get("reason") or data.get("error") or data.get("detail") if isinstance(data, dict) else None
                return _TokenDenied(detail if isinstance(detail, str) and len(detail) < 80 else "invalid_client")
            self._basic_tokens[cache_key] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
            log(logger, logging.INFO, "token issued for basic-auth client", client_id=client_id,
                token=mask_key(data["access_token"]), expires_in=data.get("expires_in"))
            return data["access_token"]

    @staticmethod
    def _basic_credentials(scope: Scope) -> Optional[tuple[str, str]]:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, encoded = value.decode("latin-1").partition(" ")
                if scheme.lower() != "basic":
                    return None
                try:
                    client_id, sep, client_secret = base64.b64decode(encoded.strip(), validate=True).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    return ("", "")  # malformed -> rejected as invalid_client
                return (client_id, client_secret) if sep and client_id and client_secret else ("", "")
        return None

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

    async def _send_resource_metadata(self, scope: Scope, send: Send) -> None:
        """Public RFC 9728 document; CORS-open so browser-based clients can read it too."""
        cors = [(b"access-control-allow-origin", b"*"), (b"access-control-allow-methods", b"GET, OPTIONS"),
                (b"access-control-allow-headers", b"*")]
        if scope["method"] == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": cors})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["method"] not in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 405, "headers": [(b"allow", b"GET, OPTIONS")] + cors})
            await send({"type": "http.response.body", "body": b""})
            return
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(self.resource_metadata)).encode()),
                   (b"cache-control", b"public, max-age=300")] + cors
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else self.resource_metadata})

    def _www_authenticate(self, reason: str) -> bytes:
        """Every 401 points at the RFC 9728 metadata so OAuth clients can discover AuthForge."""
        value = f'Bearer resource_metadata="{self.resource_metadata_url}"'
        if reason not in ("missing_token",):
            # RFC 6750: error attributes only when credentials were presented
            value += f', error="invalid_token", error_description="{reason}"'
        return value.encode()

    async def _reject(self, send: Send, status: int, reason: str, message: str) -> None:
        body = json.dumps({"error": "unauthorized" if status == 401 else "service_unavailable",
                           "reason": reason, "message": message}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if status == 401:
            headers.append((b"www-authenticate", self._www_authenticate(reason)))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
