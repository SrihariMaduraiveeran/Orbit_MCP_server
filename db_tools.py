"""PostgreSQL tools: find tables ending with "ai" and read / download their enriched-data column."""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Iterator, Literal, Optional
from urllib.parse import quote

import psycopg
from btp_connectivity import ConnectivityError, LocalForwarder, get_destination, onpremise_forwarder
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from psycopg import sql
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

TABLE_SUFFIX = os.getenv("DB_TABLE_SUFFIX", "ai")
ENRICHED_COLUMN = os.getenv("DB_ENRICHED_COLUMN", "enriched_data")
DB_SCHEMA = os.getenv("DB_SCHEMA") or None  # None = every non-system schema
STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "300000"))
MAX_PAGE_ROWS = 1000
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "exports"))

# Set by the server entry point when running over HTTP; None means stdio, where exports go to EXPORT_DIR.
public_base_url: Optional[str] = None

_EPHEMERAL_KEY = secrets.token_bytes(32)


# --- Connection -------------------------------------------------------------------------------

DB_DESTINATION_NAME = os.getenv("DB_DESTINATION_NAME") or None  # BTP destination (Type TCP) for the DB


def get_conninfo() -> Optional[str]:
    """DATABASE_URL, or DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD / DB_SSLMODE."""
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if not (os.getenv("DB_HOST") or DB_DESTINATION_NAME):
        return None
    return psycopg.conninfo.make_conninfo(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
    )


def network_route() -> tuple[dict, Optional[LocalForwarder]]:
    """Host/port overrides for psycopg, plus the Cloud Connector forwarder when the DB is on-premise.

    With DB_DESTINATION_NAME the address and proxy type come from the BTP destination; otherwise
    DB_PROXY_TYPE=OnPremise (with DB_HOST / DB_PORT / DB_LOCATION_ID) selects Cloud Connector.
    """
    if DB_DESTINATION_NAME:
        dest = get_destination(DB_DESTINATION_NAME)
        host, _, port = dest["Address"].rpartition(":")
        proxy_type = dest.get("ProxyType", "Internet")
        location_id = dest.get("CloudConnectorLocationId") or None
    else:
        host, port = os.getenv("DB_HOST", ""), os.getenv("DB_PORT", "5432")
        proxy_type = os.getenv("DB_PROXY_TYPE", "Internet")
        location_id = os.getenv("DB_LOCATION_ID") or None

    if proxy_type.lower() != "onpremise":
        return {"host": host, "port": port}, None
    forwarder = onpremise_forwarder(host, int(port), location_id)
    return {"host": "127.0.0.1", "port": str(forwarder.port)}, forwarder


def connect() -> psycopg.Connection:
    conninfo = get_conninfo()
    if not conninfo:
        raise ToolError("Database is not configured. Set DATABASE_URL, DB_HOST or DB_DESTINATION_NAME.")
    forwarder = None
    try:
        overrides = {}
        if not os.getenv("DATABASE_URL"):
            overrides, forwarder = network_route()
        # Every session is read-only, so these tools can never modify data.
        return psycopg.connect(
            conninfo,
            connect_timeout=10,
            options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
            **overrides,
        )
    except ConnectivityError as e:
        raise ToolError(f"Could not reach the database through BTP connectivity: {e}") from e
    except psycopg.Error as e:
        # A tunnel failure surfaces in psycopg only as "connection closed"; report the real cause.
        if forwarder and forwarder.last_error:
            raise ToolError(f"Could not reach the database through Cloud Connector: {forwarder.last_error}") from e
        raise ToolError(f"Could not connect to the database: {e}") from e


# --- Table / column discovery -----------------------------------------------------------------

def normalize(name: str) -> str:
    """"Enriched Data", "enriched_data" and "EnrichedData" all become "enricheddata"."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_ai_tables(conn: psycopg.Connection, schema: Optional[str]) -> list[dict]:
    """Tables and views the user can read whose name ends with TABLE_SUFFIX (case-insensitive)."""
    rows = conn.execute(
        """
        SELECT t.table_schema, t.table_name, t.table_type, c.column_name, c.data_type
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE right(lower(t.table_name), length(%(suffix)s)) = lower(%(suffix)s)
          AND t.table_schema NOT IN ('pg_catalog', 'information_schema')
          AND t.table_schema NOT LIKE 'pg\\_%%'
          AND (%(schema)s::text IS NULL OR t.table_schema = %(schema)s)
        ORDER BY t.table_schema, t.table_name, c.ordinal_position
        """,
        {"suffix": TABLE_SUFFIX, "schema": schema},
    ).fetchall()

    tables: dict[tuple[str, str], dict] = {}
    for table_schema, table_name, table_type, column_name, data_type in rows:
        entry = tables.setdefault(
            (table_schema, table_name),
            {"schema": table_schema, "table": table_name, "type": table_type, "columns": {}},
        )
        entry["columns"][column_name] = data_type

    wanted = normalize(ENRICHED_COLUMN)
    for entry in tables.values():
        matches = [c for c in entry["columns"] if normalize(c) == wanted]
        entry["enriched_column"] = matches[0] if matches else None
        if not matches:
            # Help the user spot a differently named column
            entry["similar_columns"] = [c for c in entry["columns"] if "enrich" in c.lower()]
    return list(tables.values())


def primary_key_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (sql.Identifier(schema, table).as_string(conn),),
    ).fetchall()
    return [r[0] for r in rows]


def resolve_table(conn: psycopg.Connection, table: str, schema: Optional[str]) -> dict:
    """Look the table up among the "ai" tables; only names found there are ever put into SQL."""
    schema = schema or DB_SCHEMA
    matches = [t for t in find_ai_tables(conn, schema) if t["table"].lower() == table.lower()]
    if not matches:
        raise ToolError(
            f"No readable table named '{table}' ending with '{TABLE_SUFFIX}'"
            + (f" in schema '{schema}'" if schema else "")
            + ". Use list_ai_tables to see the available tables."
        )
    if len(matches) > 1:
        exact = [t for t in matches if t["table"] == table]
        if len(exact) == 1:
            matches = exact
        else:
            schemas = ", ".join(sorted({t["schema"] for t in matches}))
            raise ToolError(f"Table '{table}' exists in several schemas ({schemas}). Pass the schema argument.")
    target = matches[0]
    if not target["enriched_column"]:
        similar = target.get("similar_columns") or []
        raise ToolError(
            f"Table {target['schema']}.{target['table']} has no '{ENRICHED_COLUMN}' column"
            + (f" (similar: {', '.join(similar)}). Set DB_ENRICHED_COLUMN to the right name." if similar else ".")
        )
    target["id_columns"] = primary_key_columns(conn, target["schema"], target["table"])
    return target


def build_select(target: dict, include_id: bool, non_null_only: bool) -> tuple[sql.Composed, list[str]]:
    columns = (target["id_columns"] if include_id else []) + [target["enriched_column"]]
    query = sql.SQL("SELECT {cols} FROM {table}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        table=sql.Identifier(target["schema"], target["table"]),
    )
    if non_null_only:
        query += sql.SQL(" WHERE {col} IS NOT NULL").format(col=sql.Identifier(target["enriched_column"]))
    if target["id_columns"]:
        # Stable order so paging and exports are repeatable
        query += sql.SQL(" ORDER BY {ids}").format(
            ids=sql.SQL(", ").join(sql.Identifier(c) for c in target["id_columns"])
        )
    return query, columns


# --- Value formatting -------------------------------------------------------------------------

def to_json_value(value):
    """psycopg returns dict/list for json(b); everything else (Decimal, datetime, UUID...) becomes a string."""
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    if isinstance(value, (bytes, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    return str(value)


def to_csv_value(value) -> str:
    value = to_json_value(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def generate_export(target: dict, include_id: bool, non_null_only: bool, fmt: str) -> Iterator[bytes]:
    """Stream the column as CSV or JSON using a server-side cursor, so large tables never sit in memory."""
    query, columns = build_select(target, include_id, non_null_only)
    with connect() as conn, conn.cursor(name="enriched_export") as cur:
        cur.itersize = 2000
        cur.execute(query)
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(columns)
            for i, row in enumerate(cur, 1):
                writer.writerow([to_csv_value(v) for v in row])
                if i % 2000 == 0:
                    yield buf.getvalue().encode()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue().encode()
        else:
            yield b"["
            for i, row in enumerate(cur):
                record = {c: to_json_value(v) for c, v in zip(columns, row)}
                yield (("," if i else "") + "\n" + json.dumps(record, ensure_ascii=False, default=str)).encode()
            yield b"\n]\n"


# --- Signed download links --------------------------------------------------------------------

def _signing_key() -> bytes:
    secret = os.getenv("DOWNLOAD_SIGNING_KEY") or os.getenv("MCP_API_KEY")
    if secret:
        return hmac.new(secret.encode(), b"enriched-download-links", hashlib.sha256).digest()
    # No shared secret: links only work on this process (fine for local use)
    return _EPHEMERAL_KEY


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_token(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())


def verify_token(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if payload.get("exp", 0) >= time.time() else None


def export_filename(target: dict, fmt: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{target['table']}_{target['enriched_column']}")
    return f"{stem}.{fmt}"


# --- MCP tools and routes ---------------------------------------------------------------------

def register_db_tools(mcp: FastMCP) -> None:

    @mcp.tool
    def list_ai_tables(schema: Optional[str] = None) -> dict:
        """List PostgreSQL tables whose name ends with "ai" and show which have the enriched-data column.

        Args:
            schema: Only look in this schema. Defaults to DB_SCHEMA, or every non-system schema.
        """
        with connect() as conn:
            try:
                tables = find_ai_tables(conn, schema or DB_SCHEMA)
                for t in tables:
                    t["estimated_rows"] = conn.execute(
                        "SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = %s::regclass",
                        (sql.Identifier(t["schema"], t["table"]).as_string(conn),),
                    ).fetchone()[0]
                    t["column_count"] = len(t.pop("columns"))
            except psycopg.Error as e:
                raise ToolError(f"Database error listing tables: {e}") from e
        return {
            "table_suffix": TABLE_SUFFIX,
            "enriched_column_searched": ENRICHED_COLUMN,
            "table_count": len(tables),
            "tables": tables,
        }

    @mcp.tool
    def get_enriched_data(
        table: str,
        schema: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        include_id: bool = True,
        non_null_only: bool = False,
    ) -> dict:
        """Read rows of the enriched-data column from one "ai" table, a page at a time.

        Args:
            table: Table name, as returned by list_ai_tables.
            schema: Schema of the table, if the name exists in several schemas.
            limit: Rows per page (max 1000).
            offset: Rows to skip, for paging.
            include_id: Also return the primary-key column(s) so each value can be traced to its row.
            non_null_only: Skip rows where the enriched column is empty (NULL).
        """
        limit = max(1, min(limit, MAX_PAGE_ROWS))
        with connect() as conn:
            try:
                target = resolve_table(conn, table, schema)
                query, columns = build_select(target, include_id, non_null_only)
                query += sql.SQL(" LIMIT {} OFFSET {}").format(sql.Literal(limit), sql.Literal(max(0, offset)))
                rows = conn.execute(query).fetchall()
            except psycopg.Error as e:
                raise ToolError(f"Database error reading {table}: {e}") from e
        return {
            "schema": target["schema"],
            "table": target["table"],
            "enriched_column": target["enriched_column"],
            "columns": columns,
            "offset": offset,
            "row_count": len(rows),
            "has_more": len(rows) == limit,
            "rows": [{c: to_json_value(v) for c, v in zip(columns, row)} for row in rows],
        }

    @mcp.tool
    def export_enriched_data(
        table: str,
        schema: Optional[str] = None,
        format: Literal["csv", "json"] = "csv",
        include_id: bool = True,
        non_null_only: bool = False,
        expires_in: int = 3600,
    ) -> dict:
        """Export the whole enriched-data column of an "ai" table as a CSV or JSON file to download.

        Over HTTP this returns a temporary download link served by this server (nothing is uploaded
        anywhere). In local stdio mode the file is written to the EXPORT_DIR folder instead.

        Args:
            table: Table name, as returned by list_ai_tables.
            schema: Schema of the table, if the name exists in several schemas.
            format: "csv" or "json".
            include_id: Also export the primary-key column(s).
            non_null_only: Skip rows where the enriched column is empty (NULL).
            expires_in: Seconds the download link stays valid (60 - 86400, default 1 hour).
        """
        with connect() as conn:
            try:
                target = resolve_table(conn, table, schema)
            except psycopg.Error as e:
                raise ToolError(f"Database error reading {table}: {e}") from e
        filename = export_filename(target, format)

        if public_base_url is None:
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = EXPORT_DIR / filename
            try:
                with open(path, "wb") as f:
                    for chunk in generate_export(target, include_id, non_null_only, format):
                        f.write(chunk)
            except psycopg.Error as e:
                raise ToolError(f"Database error exporting {table}: {e}") from e
            return {"schema": target["schema"], "table": target["table"], "file": str(path.resolve()),
                    "size": path.stat().st_size}

        expires_in = max(60, min(expires_in, 86400))
        token = sign_token({
            "s": target["schema"], "t": target["table"], "f": format,
            "id": include_id, "nn": non_null_only, "exp": int(time.time()) + expires_in,
        })
        return {
            "schema": target["schema"],
            "table": target["table"],
            "enriched_column": target["enriched_column"],
            "format": format,
            "filename": filename,
            "download_url": f"{public_base_url.rstrip('/')}/download/enriched?token={quote(token)}",
            "expires_in_seconds": expires_in,
        }

    @mcp.custom_route("/download/enriched", methods=["GET"])
    async def download_enriched(request: Request):
        # The signed, expiring token is the authorization: it can only have been issued by export_enriched_data.
        payload = verify_token(request.query_params.get("token", ""))
        if payload is None:
            return JSONResponse({"error": "Download link is invalid or has expired."}, status_code=403)

        def resolve():
            with connect() as conn:
                return resolve_table(conn, payload["t"], payload["s"])

        try:
            target = await run_in_threadpool(resolve)
        except (ToolError, psycopg.Error) as e:
            return JSONResponse({"error": str(e)}, status_code=404)

        fmt = payload["f"]
        return StreamingResponse(
            generate_export(target, payload["id"], payload["nn"], fmt),
            media_type="text/csv; charset=utf-8" if fmt == "csv" else "application/json",
            headers={"Content-Disposition": f'attachment; filename="{export_filename(target, fmt)}"'},
        )
