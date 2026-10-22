import json
import os
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

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


def build_auth() -> Optional[JWTVerifier]:
    """Validate XSUAA-issued JWTs when an XSUAA service is bound."""
    xsuaa = get_service_credentials("xsuaa")
    if not xsuaa:
        if ON_CLOUD_FOUNDRY and os.getenv("ALLOW_UNAUTHENTICATED", "").lower() != "true":
            raise RuntimeError(
                "No XSUAA service bound. Bind one (see xs-security.json) or set "
                "ALLOW_UNAUTHENTICATED=true to expose the server without authentication."
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


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def get_s3_client():
    # With no explicit keys, boto3 falls back to its standard chain (AWS_PROFILE, ~/.aws, IAM role).
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
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


if __name__ == "__main__":
    # Cloud Foundry sets PORT; there the server always runs over HTTP.
    transport = os.getenv("MCP_TRANSPORT", "http" if "PORT" in os.environ else "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport=transport,  # "http" or "sse"
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("PORT") or os.getenv("MCP_PORT", "8000")),
            stateless_http=True,  # no sticky sessions needed across CF instances
        )
