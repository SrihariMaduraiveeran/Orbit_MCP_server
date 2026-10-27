"""Local bridge so Claude Desktop can use the AuthForge-protected MCP server.

Claude Desktop starts this script (stdio). It forwards every MCP request to the remote server,
adding `Authorization: Bearer at_live_...`. Tokens come from AuthForge's client-credentials
grant and are renewed automatically before they expire (and once more if the server answers 401,
e.g. after a revoke) - so nothing needs to be pasted or restarted every hour.

Required environment variables (set them in claude_desktop_config.json):
  MCP_URL        https://<app-route>/mcp
  AUTHFORGE_URL  https://<authforge-host>
  CLIENT_ID      client_...
  CLIENT_SECRET  cs_...

Requires: pip install fastmcp
"""

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy


def log(message: str) -> None:
    # stdout carries the MCP protocol; diagnostics must go to stderr (Claude Desktop shows it in its MCP logs)
    print(f"[authforge-bridge] {message}", file=sys.stderr, flush=True)


class AuthForgeTokenAuth(httpx2.Auth):
    """Adds a valid AuthForge access token to each request, fetching a new one when needed."""

    def __init__(self, authforge_url: str, client_id: str, client_secret: str):
        self.token_url = f"{authforge_url.rstrip('/')}/api/oauth/token"
        self.basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires_at - 120:
                return self._token
            req = urllib.request.Request(
                self.token_url,
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={"Authorization": f"Basic {self.basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.load(resp)
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"AuthForge token request failed (HTTP {e.code}): check CLIENT_ID / CLIENT_SECRET") from e
            except OSError as e:
                raise RuntimeError(f"AuthForge unreachable at {self.token_url}: {e}") from e
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))
            log(f"new access token {self._token[:12]}... valid for {data.get('expires_in', 3600)}s")
            return self._token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        response = yield request
        if response.status_code == 401:
            # Token revoked or expired early: get a fresh one and retry once
            log("server answered 401 - refreshing token and retrying")
            request.headers["Authorization"] = f"Bearer {self._get_token(force=True)}"
            yield request


def main() -> None:
    missing = [v for v in ("MCP_URL", "AUTHFORGE_URL", "CLIENT_ID", "CLIENT_SECRET") if not os.getenv(v)]
    if missing:
        log(f"missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    auth = AuthForgeTokenAuth(os.environ["AUTHFORGE_URL"], os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"])
    remote = Client(StreamableHttpTransport(os.environ["MCP_URL"], auth=auth))
    proxy = create_proxy(remote, name=os.getenv("BRIDGE_NAME", "orbit"))
    log(f"forwarding to {os.environ['MCP_URL']}")
    proxy.run(show_banner=False)


if __name__ == "__main__":
    main()
