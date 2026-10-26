import base64
import hmac
import json
import mimetypes
import os
from typing import Optional
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.types import BlobResourceContents, EmbeddedResource
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

import db_tools  # noqa: E402  (reads its settings from the environment loaded above)
from authforge import AuthForgeMiddleware, authforge_enabled  # noqa: E402
from db_tools import register_db_tools  # noqa: E402

ON_CLOUD_FOUNDRY = "VCAP_APPLICATION" in os.environ


def get_service_credentials(label: str) -> Optional[dict]:
    """Return the credentials of the first service bound to this app with the given label (BTP / Cloud Foundry)."""
    services = json.loads(os.getenv("VCAP_SERVICES", "{}"))
    for instance in services.get(label, []):
        return instance.get("credentials", {})
    # Fall back to user-provided services tagged with the label
    for instance in services.get("user-provided", []):
        if label in instance.get("tags", []):
            return instance.get("credentials", {})
    return None


# --- S3 configuration: bound SAP Object Store service on BTP, .env / environment locally ---
_objectstore = get_service_credentials("objectstore") or {}

AWS_REGION = _objectstore.get("region") or os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = _objectstore.get("bucket") or os.getenv("S3_BUCKET_NAME")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL") or None  # for S3-compatible stores (MinIO, ...)
S3_ACCESS_KEY_ID = _objectstore.get("access_key_id") or os.getenv("AWS_ACCESS_KEY_ID") or None
S3_SECRET_ACCESS_KEY = _objectstore.get("secret_access_key") or os.getenv("AWS_SECRET_ACCESS_KEY") or None
MAX_ITEMS_DEFAULT = int(os.getenv("S3_MAX_ITEMS", "1000"))
MAX_READ_BYTES = int(os.getenv("S3_MAX_READ_BYTES", str(5 * 1024 * 1024)))


class APIKeyVerifier(TokenVerifier):
    """Accepts requests whose bearer token equals the configured API key."""

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not hmac.compare_digest(token.encode(), self.api_key.encode()):
            return None
        return AccessToken(token="api-key", client_id="api-key-client", scopes=[])


def build_auth() -> Optional[TokenVerifier]:
    """XSUAA JWTs when an XSUAA service is bound, otherwise a static API key (MCP_API_KEY)."""
    if authforge_enabled():
        return None  # AuthForgeMiddleware authenticates every HTTP request instead
    xsuaa = get_service_credentials("xsuaa")
    if not xsuaa:
        api_key = os.getenv("MCP_API_KEY")
        if api_key:
            return APIKeyVerifier(api_key)
        if ON_CLOUD_FOUNDRY and os.getenv("ALLOW_UNAUTHENTICATED", "").lower() != "true":
            raise RuntimeError(
                "No authentication configured. Set MCP_API_KEY, bind an XSUAA service "
                "(see xs-security.json), or set ALLOW_UNAUTHENTICATED=true."
            )
        return None

    uaa_url = xsuaa["url"].rstrip("/")
    return JWTVerifier(
        jwks_uri=f"{uaa_url}/token_keys",
        issuer=f"{uaa_url}/oauth/token",
        audience=[xsuaa["clientid"], xsuaa["xsappname"]],
        algorithm="RS256",
        required_scopes=[f"{xsuaa['xsappname']}.Read"],
    )


mcp = FastMCP("S3 Explorer", auth=build_auth())
register_db_tools(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def get_s3_client():
    # With no explicit keys, boto3 falls back to its standard chain (AWS_PROFILE, ~/.aws, IAM role).
    # Regional endpoint so pre-signed URLs work for buckets outside us-east-1
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        endpoint_url=S3_ENDPOINT_URL or f"https://s3.{AWS_REGION}.amazonaws.com",
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def resolve_bucket(bucket: Optional[str]) -> str:
    name = bucket or S3_BUCKET_NAME
    if not name:
        raise ToolError("No bucket given and no default bucket is configured.")
    return name


def normalize_prefix(prefix: str) -> str:
    prefix = (prefix or "").lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def file_entry(obj: dict) -> dict:
    return {
        "key": obj["Key"],
        "size": obj["Size"],
        "last_modified": obj["LastModified"].isoformat(),
        "storage_class": obj.get("StorageClass"),
    }


def collect(bucket: str, prefix: str, recursive: bool, max_items: int) -> dict:
    s3 = get_s3_client()
    params = {"Bucket": bucket, "Prefix": prefix}
    if not recursive:
        params["Delimiter"] = "/"

    folders: set[str] = set()
    files: list[dict] = []
    truncated = False

    try:
        for page in s3.get_paginator("list_objects_v2").paginate(**params):
            for cp in page.get("CommonPrefixes", []):
                folders.add(cp["Prefix"])
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    # Zero-byte "folder marker" objects created by the S3 console
                    if key != prefix:
                        folders.add(key)
                    continue
                files.append(file_entry(obj))
                if recursive:
                    # Derive every intermediate folder from the key path
                    parts = key[len(prefix):].split("/")[:-1]
                    for i in range(1, len(parts) + 1):
                        folders.add(prefix + "/".join(parts[:i]) + "/")
            if len(files) + len(folders) >= max_items:
                truncated = True
                break
    except (ClientError, BotoCoreError) as e:
        raise ToolError(f"S3 error listing s3://{bucket}/{prefix}: {e}") from e

    return {
        "bucket": bucket,
        "prefix": prefix,
        "recursive": recursive,
        "folder_count": len(folders),
        "file_count": len(files),
        "truncated": truncated,
        "folders": sorted(folders),
        "files": files[:max_items],
    }


@mcp.tool
def list_buckets() -> list[dict]:
    """List all S3 buckets the configured credentials can access."""
    try:
        resp = get_s3_client().list_buckets()
    except (ClientError, BotoCoreError) as e:
        raise ToolError(f"S3 error listing buckets: {e}") from e
    return [
        {"name": b["Name"], "created": b["CreationDate"].isoformat()}
        for b in resp.get("Buckets", [])
    ]


@mcp.tool
def list_folders_and_files(
    bucket: Optional[str] = None,
    prefix: str = "",
    recursive: bool = True,
    max_items: int = MAX_ITEMS_DEFAULT,
) -> dict:
    """List folders and files in an S3 bucket.

    Args:
        bucket: Bucket name. Defaults to the configured bucket.
        prefix: Folder path to start from, e.g. "reports/2025/". Empty means bucket root.
        recursive: True walks every sub-folder; False lists only the immediate level.
        max_items: Stop after this many folders + files (result is marked truncated).
    """
    return collect(resolve_bucket(bucket), normalize_prefix(prefix), recursive, max_items)


@mcp.tool
def get_folder_tree(
    bucket: Optional[str] = None,
    prefix: str = "",
    max_items: int = MAX_ITEMS_DEFAULT,
) -> dict:
    """Return the bucket contents as a nested folder tree.

    Each folder node has "folders" (name -> node) and "files" (list of file names with size).
    """
    data = collect(resolve_bucket(bucket), normalize_prefix(prefix), True, max_items)
    root: dict = {"folders": {}, "files": []}

    def node_for(parts: list[str]) -> dict:
        node = root
        for part in parts:
            node = node["folders"].setdefault(part, {"folders": {}, "files": []})
        return node

    base = len(data["prefix"])
    for folder in data["folders"]:
        node_for(folder[base:].rstrip("/").split("/"))
    for f in data["files"]:
        *dirs, name = f["key"][base:].split("/")
        node_for(dirs)["files"].append({"name": name, "size": f["size"]})

    return {
        "bucket": data["bucket"],
        "prefix": data["prefix"],
        "truncated": data["truncated"],
        "tree": root,
    }


@mcp.tool
def get_object_info(key: str, bucket: Optional[str] = None) -> dict:
    """Get metadata (size, content type, last modified, ETag) for a single S3 object."""
    bucket = resolve_bucket(bucket)
    try:
        h = get_s3_client().head_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as e:
        raise ToolError(f"S3 error reading s3://{bucket}/{key}: {e}") from e
    return {
        "bucket": bucket,
        "key": key,
        "size": h["ContentLength"],
        "content_type": h.get("ContentType"),
        "last_modified": h["LastModified"].isoformat(),
        "etag": h.get("ETag", "").strip('"'),
        "metadata": h.get("Metadata", {}),
    }


@mcp.tool
def get_download_url(key: str, bucket: Optional[str] = None, expires_in: int = 3600) -> dict:
    """Create a temporary pre-signed URL to download an S3 file (any size) directly from S3.

    Args:
        key: Full object key, e.g. "reports/2025/q1.csv".
        bucket: Bucket name. Defaults to the configured bucket.
        expires_in: Seconds the link stays valid (60 - 604800, default 1 hour).
    """
    bucket = resolve_bucket(bucket)
    expires_in = max(60, min(expires_in, 604800))
    s3 = get_s3_client()
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{key.rsplit("/", 1)[-1]}"',
            },
            ExpiresIn=expires_in,
        )
    except (ClientError, BotoCoreError) as e:
        raise ToolError(f"S3 error preparing download for s3://{bucket}/{key}: {e}") from e
    return {
        "bucket": bucket,
        "key": key,
        "size": head["ContentLength"],
        "download_url": url,
        "expires_in_seconds": expires_in,
    }


@mcp.tool
def read_file(key: str, bucket: Optional[str] = None, max_bytes: int = MAX_READ_BYTES):
    """Download an S3 file and return its contents: text for text files, an embedded file otherwise.

    Use get_download_url for large files.

    Args:
        key: Full object key, e.g. "reports/2025/q1.csv".
        bucket: Bucket name. Defaults to the configured bucket.
        max_bytes: Refuse files larger than this (default from S3_MAX_READ_BYTES, 5 MB).
    """
    bucket = resolve_bucket(bucket)
    s3 = get_s3_client()
    try:
        size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        if size > max_bytes:
            raise ToolError(
                f"s3://{bucket}/{key} is {size} bytes, over the {max_bytes}-byte limit. "
                "Use get_download_url instead."
            )
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except (ClientError, BotoCoreError) as e:
        raise ToolError(f"S3 error downloading s3://{bucket}/{key}: {e}") from e

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        mime_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=f"s3://{bucket}/{quote(key)}",
                mimeType=mime_type,
                blob=base64.b64encode(data).decode(),
            ),
        )


def public_base_url(port: int) -> str:
    """URL clients use to reach this server: PUBLIC_BASE_URL, the app's CF route, or localhost."""
    if os.getenv("PUBLIC_BASE_URL"):
        return os.environ["PUBLIC_BASE_URL"]
    uris = json.loads(os.getenv("VCAP_APPLICATION", "{}")).get("application_uris", [])
    if uris:
        return f"https://{uris[0]}"
    return f"http://localhost:{port}"


if __name__ == "__main__":
    # Cloud Foundry sets PORT; there the server always runs over HTTP.
    transport = os.getenv("MCP_TRANSPORT", "http" if "PORT" in os.environ else "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        if authforge_enabled() and not os.getenv("AUTHFORGE_URL"):
            raise SystemExit("AUTHFORGE_URL is not set. Set it, or AUTHFORGE_DISABLED=true for local development.")
        port = int(os.getenv("PORT") or os.getenv("MCP_PORT", "8000"))
        db_tools.public_base_url = public_base_url(port)  # enables download links for DB exports
        mcp.run(
            transport=transport,  # "http" or "sse"
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=port,
            stateless_http=True,  # no sticky sessions needed across CF instances
            middleware=[Middleware(AuthForgeMiddleware)] if authforge_enabled() else None,
        )
