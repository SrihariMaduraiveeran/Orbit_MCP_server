"""Application logging: one line per auth decision, tool call and download, written to stdout
(so it shows up in `cf logs`). Secrets are never logged; API keys are masked.

LOG_LEVEL  = DEBUG | INFO (default) | WARNING | ERROR
LOG_FORMAT = text (default) | json   (json suits SAP Cloud Logging / Kibana)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

LOGGER_NAME = "mcp_app"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = getattr(record, "fields", {})
        if fields:
            line += " | " + " ".join(f"{k}={_text_value(v)}" for k, v in fields.items())
        return line


def _text_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return f'"{text}"' if " " in text else text


def setup_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False  # uvicorn reconfigures the root logger; keep ours independent
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": {k: v for k, v in fields.items() if v is not None}})


def mask_key(key: Optional[str]) -> Optional[str]:
    """at_live_abcdef...wxyz -> at_live_ab…wxyz (enough to tell tokens apart, useless to an attacker)."""
    if not key:
        return None
    if len(key) <= 14:
        return key[:4] + "…"
    return f"{key[:10]}…{key[-4:]}"


def client_info(headers: dict[str, str], fallback_ip: Optional[str] = None) -> dict[str, Optional[str]]:
    """Real client IP behind the Cloud Foundry router, plus user agent."""
    ip = headers.get("x-cf-true-client-ip") or (headers.get("x-forwarded-for") or "").split(",")[0].strip() or fallback_ip
    return {"client_ip": ip or None, "user_agent": headers.get("user-agent")}


def _caller() -> dict[str, Any]:
    try:
        request = get_http_request()
    except RuntimeError:
        return {"caller": "local-stdio"}
    auth = getattr(request.state, "authforge", None) or {}
    return {
        "app_id": auth.get("app_id"),
        "client_id": auth.get("client_id"),
        "client_name": auth.get("client_name"),
        "scope": auth.get("scope"),
        **client_info({k.lower(): v for k, v in request.headers.items()}, request.client.host if request.client else None),
    }


def _short(value: Any, limit: int = 200) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class ToolLoggingMiddleware(Middleware):
    """Logs every MCP tool call: tool, arguments, caller, duration, outcome."""

    def __init__(self):
        self.logger = get_logger("tools")

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        args = {k: _short(v) for k, v in (context.message.arguments or {}).items()}
        caller = _caller()
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as e:
            log(self.logger, logging.WARNING, f"tool {name} failed", tool=name, args=args,
                duration_ms=round((time.perf_counter() - start) * 1000), error=_short(str(e), 500), **caller)
            raise
        duration = round((time.perf_counter() - start) * 1000)
        if getattr(result, "is_error", False):
            log(self.logger, logging.WARNING, f"tool {name} returned an error", tool=name, args=args,
                duration_ms=duration, **caller)
        else:
            log(self.logger, logging.INFO, f"tool {name} ok", tool=name, args=args, duration_ms=duration, **caller)
        return result
