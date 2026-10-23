"""Reach on-premise TCP systems (e.g. PostgreSQL) from SAP BTP Cloud Foundry via the Connectivity
service and Cloud Connector.

The Connectivity service exposes a SOCKS5 proxy that requires SAP's custom authentication
(a JWT plus the Cloud Connector location ID). Database drivers cannot speak that, so a local
forwarder listens on 127.0.0.1 and tunnels every accepted connection through the proxy.
"""

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional


def get_service_credentials(label: str) -> Optional[dict]:
    services = json.loads(os.getenv("VCAP_SERVICES", "{}"))
    for instance in services.get(label, []):
        return instance.get("credentials", {})
    for instance in services.get("user-provided", []):
        if label in instance.get("tags", []):
            return instance.get("credentials", {})
    return None


class ConnectivityError(Exception):
    pass


# --- OAuth client-credentials tokens (cached until shortly before expiry) ---------------------

class TokenCache:
    def __init__(self, token_url: str, client_id: str, client_secret: str):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id}).encode()
            auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            req = urllib.request.Request(
                self.token_url, data=body,
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.load(resp)
            except OSError as e:
                raise ConnectivityError(f"Could not get a token from {self.token_url}: {e}") from e
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))
            return self._token


def token_url_for(credentials: dict) -> str:
    base = (credentials.get("token_service_url") or credentials["url"]).rstrip("/")
    return base if base.endswith("/oauth/token") else base + "/oauth/token"


_token_caches: dict[str, TokenCache] = {}


def token_for(credentials: dict) -> str:
    key = credentials["clientid"]
    if key not in _token_caches:
        _token_caches[key] = TokenCache(token_url_for(credentials), credentials["clientid"], credentials["clientsecret"])
    return _token_caches[key].get()


# --- Destination service ----------------------------------------------------------------------

_destination_cache: dict[str, tuple[float, dict]] = {}


def get_destination(name: str, ttl: int = 300) -> dict:
    """Return the destinationConfiguration of a BTP destination (cached for `ttl` seconds)."""
    cached = _destination_cache.get(name)
    if cached and time.time() < cached[0]:
        return cached[1]
    creds = get_service_credentials("destination")
    if not creds:
        raise ConnectivityError(f"Destination '{name}' is configured but no Destination service is bound to the app.")
    url = f"{creds['uri'].rstrip('/')}/destination-configuration/v1/destinations/{urllib.parse.quote(name)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token_for(creds)}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            config = json.load(resp)["destinationConfiguration"]
    except OSError as e:
        raise ConnectivityError(f"Could not read destination '{name}': {e}") from e
    _destination_cache[name] = (time.time() + ttl, config)
    return config


# --- SOCKS5 with SAP authentication -----------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectivityError("Connectivity proxy closed the connection during the handshake.")
        data += chunk
    return data


_SOCKS_ERRORS = {
    1: "general failure", 2: "connection not allowed", 3: "network unreachable",
    4: "host unreachable (check the Cloud Connector virtual host/port and that it is connected)",
    5: "connection refused by the on-premise system", 6: "TTL expired",
    7: "command not supported", 8: "address type not supported",
}


def open_socks_tunnel(proxy_host: str, proxy_port: int, token: str,
                      target_host: str, target_port: int, location_id: Optional[str]) -> socket.socket:
    sock = socket.create_connection((proxy_host, proxy_port), timeout=15)
    try:
        # Greeting: SOCKS5, one method, 0x80 = SAP custom authentication
        sock.sendall(b"\x05\x01\x80")
        if _recv_exact(sock, 2) != b"\x05\x80":
            raise ConnectivityError("Connectivity proxy rejected SAP authentication.")

        # Auth: version 1, 4-byte JWT length, JWT, 1-byte location length, base64(location ID)
        jwt = token.encode()
        loc = base64.b64encode(location_id.encode()) if location_id else b""
        sock.sendall(b"\x01" + struct.pack(">I", len(jwt)) + jwt + struct.pack("B", len(loc)) + loc)
        if _recv_exact(sock, 2)[1] != 0:
            raise ConnectivityError("Connectivity proxy authentication failed (token or location ID invalid).")

        # CONNECT to the Cloud Connector virtual host
        host = target_host.encode()
        sock.sendall(b"\x05\x01\x00\x03" + struct.pack("B", len(host)) + host + struct.pack(">H", target_port))
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise ConnectivityError(
                f"Cloud Connector could not reach {target_host}:{target_port}: "
                f"{_SOCKS_ERRORS.get(reply[1], f'error {reply[1]}')}."
            )
        atyp = reply[3]
        _recv_exact(sock, {1: 4, 4: 16}.get(atyp) or _recv_exact(sock, 1)[0])  # bound address
        _recv_exact(sock, 2)  # bound port
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


# --- Local forwarder --------------------------------------------------------------------------

def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


class LocalForwarder:
    """Listens on 127.0.0.1 and connects each client through `open_upstream()`."""

    def __init__(self, open_upstream: Callable[[], socket.socket]):
        self.open_upstream = open_upstream
        self.last_error: Optional[str] = None
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True, name="connectivity-forwarder").start()

    def _accept_loop(self) -> None:
        while True:
            client, _ = self._server.accept()
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = self.open_upstream()
        except Exception as e:
            self.last_error = str(e)
            client.close()
            return
        self.last_error = None
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        _pipe(upstream, client)


_forwarders: dict[tuple, LocalForwarder] = {}
_forwarders_lock = threading.Lock()


def onpremise_forwarder(target_host: str, target_port: int, location_id: Optional[str]) -> LocalForwarder:
    """A local forwarder that reaches target_host:target_port through Cloud Connector."""
    conn = get_service_credentials("connectivity")
    if not conn:
        raise ConnectivityError(
            "The database is on-premise (ProxyType OnPremise) but no Connectivity service is bound to the app."
        )
    key = (target_host, target_port, location_id)
    with _forwarders_lock:
        if key not in _forwarders:
            proxy_host = conn["onpremise_proxy_host"]
            proxy_port = int(conn.get("onpremise_socks5_proxy_port") or conn["onpremise_proxy_socks5_port"])
            _forwarders[key] = LocalForwarder(
                lambda: open_socks_tunnel(proxy_host, proxy_port, token_for(conn), target_host, target_port, location_id)
            )
        return _forwarders[key]
