# Connecting to the Orbit MCP Server

This guide shows how to call the Orbit MCP server from **Postman** and from **Python**. The server lists and downloads files from the S3 bucket and reads the enriched-data column from the PostgreSQL tables whose names end with `ai`.

Access is secured with **AuthForge OAuth 2.0 (client credentials)**. You swap your client ID and secret for a short-lived access token, then send that token with every request.

---

## 1. What you need

| Item | Value |
|---|---|
| **Server URL** | `https://s3-mcp-server-terrific-bear-cx.cfapps.us10-001.hana.ondemand.com/mcp` |
| **Token URL** | `https://authforge.cfapps.us10-001.hana.ondemand.com/api/oauth/token` |
| **Client ID** | Your AuthForge client ID, starting with `client_`. Get it from the AuthForge dashboard or the server owner |
| **Client secret** | Your AuthForge client secret, starting with `cs_` |
| **Health check** | `https://s3-mcp-server-terrific-bear-cx.cfapps.us10-001.hana.ondemand.com/health` (public, no token) |

> **Keep your client secret private.** Don't commit it to git, paste it into chats, or hard-code it in shared scripts. Use environment variables or Postman *secret* variables.

### How it works

```
1. POST /api/oauth/token  (client ID + secret)  ──▶  AuthForge  ──▶  access token "at_live_..." (valid 1 hour)
2. POST /mcp  with  Authorization: Bearer at_live_...  ──▶  MCP server  ──▶  result
```

### Rules every MCP request must follow

1. **Method:** `POST` to `/mcp`
2. **Headers**, all three required:

   | Header | Value |
   |---|---|
   | `Authorization` | `Bearer at_live_your_access_token` |
   | `Content-Type` | `application/json` |
   | `Accept` | `application/json, text/event-stream` |

   With only `application/json` in `Accept`, the server answers **406 Not Acceptable**.

3. **Body:** a JSON-RPC 2.0 message. You don't need an `initialize` call or a session ID; every request stands on its own.
4. **Response format:** server-sent events. The JSON result is on the line that starts with `data:`:
   ```
   event: message
   data: {"jsonrpc":"2.0","id":1,"result":{ ... }}
   ```
5. **Token lifetime:** tokens expire after **1 hour**. After that the server answers `401`, and you need a new token.

---

## 2. Available tools

### S3 bucket

| Tool | What it does | Arguments |
|---|---|---|
| `list_buckets` | Lists the buckets the server can access | none |
| `list_folders_and_files` | Lists folders and files | `prefix` (folder, e.g. `"reports/"`), `recursive` (`true`/`false`), `max_items` |
| `get_folder_tree` | Returns the contents as a nested tree | `prefix`, `max_items` |
| `get_object_info` | Returns a file's size, type and date | `key` (full path, e.g. `"reports/q1.csv"`) |
| `get_download_url` | Returns a temporary download link for a file | `key`, `expires_in` (seconds, default 3600) |
| `read_file` | Returns a file's contents (up to 5 MB) | `key` |

### PostgreSQL

| Tool | What it does | Arguments |
|---|---|---|
| `list_ai_tables` | Lists tables whose names end with `ai` | `schema` (optional) |
| `get_enriched_data` | Reads the enriched-data column, one page at a time | `table` (required), `schema`, `limit` (max 1000), `offset`, `include_id`, `non_null_only` |
| `export_enriched_data` | Returns a download link for the whole column as a file | `table` (required), `schema`, `format` (`"csv"` or `"json"`), `include_id`, `non_null_only`, `expires_in` |

---

## 3. Postman

### Step 1: Create an environment

1. Click **Environments → +**, and name it `Orbit MCP`.
2. Add these variables:

   | Variable | Type | Value |
   |---|---|---|
   | `mcp_url` | default | `https://s3-mcp-server-terrific-bear-cx.cfapps.us10-001.hana.ondemand.com/mcp` |
   | `token_url` | default | `https://authforge.cfapps.us10-001.hana.ondemand.com/api/oauth/token` |
   | `client_id` | default | `client_your_id` |
   | `client_secret` | **secret** | `cs_your_secret` |

3. Save it, then select **Orbit MCP** in the environment dropdown at the top right.

### Step 2: Set up OAuth 2.0 once, on a collection

Set it on a collection so every request inherits it.

1. Create a collection named **Orbit MCP**, open it, and go to the **Authorization** tab.
2. Set **Auth Type** to **OAuth 2.0**, and **Add auth data to** to **Request Headers**.
3. Under **Configure New Token**, fill in:

   | Field | Value |
   |---|---|
   | Token Name | `AuthForge` |
   | Grant Type | **Client Credentials** |
   | Access Token URL | `{{token_url}}` |
   | Client ID | `{{client_id}}` |
   | Client Secret | `{{client_secret}}` |
   | Scope | *(leave empty)* |
   | Client Authentication | **Send as Basic Auth header** |

4. Turn on **Auto-refresh token**, if your Postman version offers it, so expired tokens are renewed automatically.
5. Click **Get New Access Token**, then **Proceed**, then **Use Token**. The token starts with `at_live_`.
6. **Save** the collection.

> Without auto-refresh, click **Get New Access Token** again whenever you get `401`. Tokens last 1 hour.

### Step 3: Create a request in the collection

1. In the **Orbit MCP** collection, add an HTTP request: method **POST**, URL `{{mcp_url}}`.
2. **Authorization** tab: **Inherit auth from parent**.
3. **Headers** tab: add these.

   | Key | Value |
   |---|---|
   | `Accept` | `application/json, text/event-stream` |
   | `Content-Type` | `application/json` |

4. **Body** tab: choose **raw**, then **JSON**.

### Step 4: List the tools

Body:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list"
}
```
Click **Send**. Expect **200 OK** with a response like:
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"list_ai_tables", ...}]}}
```

### Step 5: Call a tool

The body always has this shape. Only `name` and `arguments` change:
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "TOOL_NAME",
    "arguments": { }
  }
}
```

Ready-to-use bodies:

**List the AI tables**
```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "list_ai_tables", "arguments": {}}}
```

**Read the first 50 enriched values from a table**
```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "get_enriched_data",
            "arguments": {"table": "customer_ai", "limit": 50, "offset": 0}}}
```

**Export a table's enriched column as CSV and get a download link**
```json
{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
 "params": {"name": "export_enriched_data",
            "arguments": {"table": "customer_ai", "format": "csv"}}}
```
Copy the `download_url` from the response and open it in a browser, or in a new Postman **GET** request (*Send and Download*). The link needs no token and expires after 1 hour.

**List the folders and files in the S3 bucket**
```json
{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
 "params": {"name": "list_folders_and_files",
            "arguments": {"prefix": "", "recursive": true}}}
```

**Get a download link for an S3 file**
```json
{"jsonrpc": "2.0", "id": 6, "method": "tools/call",
 "params": {"name": "get_download_url",
            "arguments": {"key": "reports/q1.csv"}}}
```

### Using Postman's MCP request type instead (Postman 11.x and later)

1. Get a token first, as in Step 2, and copy it from **Manage Tokens**.
2. Click **New → MCP**, choose **HTTP**, and paste `{{mcp_url}}`.
3. In **Authorization**, choose **Bearer Token** and paste the `at_live_` token. Or reuse the collection's OAuth 2.0 settings, if your version offers that.
4. Click **Connect**. The 9 tools appear; select one, fill in its arguments, and click **Run**.

### Reading the result

- On success, the data is in `result.structuredContent`, and also as text in `result.content[0].text`.
- When a tool fails, the response is still **200**, but `result.isError` is `true` and `result.content[0].text` explains why.

---

## 4. Python

Set your credentials as environment variables first. Never put them in the script.

```bash
# macOS / Linux
export MCP_URL="https://s3-mcp-server-terrific-bear-cx.cfapps.us10-001.hana.ondemand.com/mcp"
export TOKEN_URL="https://authforge.cfapps.us10-001.hana.ondemand.com/api/oauth/token"
export CLIENT_ID="client_your_id"
export CLIENT_SECRET="cs_your_secret"
```
```powershell
# Windows PowerShell
$env:MCP_URL = "https://s3-mcp-server-terrific-bear-cx.cfapps.us10-001.hana.ondemand.com/mcp"
$env:TOKEN_URL = "https://authforge.cfapps.us10-001.hana.ondemand.com/api/oauth/token"
$env:CLIENT_ID = "client_your_id"
$env:CLIENT_SECRET = "cs_your_secret"
```

### Getting a token (used by both options)

Save this as `authforge_token.py`. It fetches a token, keeps it, and gets a new one shortly before it expires:

```python
import os
import time

import requests

TOKEN_URL = os.environ["TOKEN_URL"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]

_token = None
_expires_at = 0.0


def get_access_token() -> str:
    """Return a valid AuthForge access token (at_live_...), refreshing it when needed."""
    global _token, _expires_at
    if _token and time.time() < _expires_at - 60:
        return _token
    resp = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),               # HTTP Basic auth
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _token = data["access_token"]
    _expires_at = time.time() + int(data.get("expires_in", 3600))
    return _token
```

```bash
pip install requests
```

### Option A (recommended): the official MCP client

```bash
pip install fastmcp
```

```python
import asyncio
import os

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from authforge_token import get_access_token

MCP_URL = os.environ["MCP_URL"]


async def main():
    transport = StreamableHttpTransport(MCP_URL, headers={"Authorization": f"Bearer {get_access_token()}"})
    async with Client(transport) as client:
        # 1. List the tools
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        # 2. List the AI tables
        result = await client.call_tool("list_ai_tables", {})
        for table in result.data["tables"]:
            print(f"{table['schema']}.{table['table']}  ->  column: {table['enriched_column']}")

        # 3. Read enriched data, one page at a time
        page = await client.call_tool("get_enriched_data", {"table": "customer_ai", "limit": 100})
        for row in page.data["rows"]:
            print(row)

        # 4. Get a download link for the whole column
        export = await client.call_tool("export_enriched_data", {"table": "customer_ai", "format": "csv"})
        print("Download:", export.data["download_url"])


asyncio.run(main())
```

`call_tool` raises an exception when a tool fails. To get the error as a value instead, pass `raise_on_error=False` and check `result.is_error`. A connection holds on to the token it started with, so open a new `Client` for jobs that run longer than an hour.

### Option B: plain `requests`, no MCP library

```python
import itertools
import json
import os

import requests

from authforge_token import get_access_token

MCP_URL = os.environ["MCP_URL"]
_ids = itertools.count(1)


def mcp_request(method: str, params: dict | None = None) -> dict:
    """Send one JSON-RPC request and return its "result"."""
    headers = {
        "Authorization": f"Bearer {get_access_token()}",   # refreshed automatically
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        body["params"] = params
    resp = requests.post(MCP_URL, headers=headers, json=body, timeout=120)

    if resp.status_code in (401, 503):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.json()}")
    resp.raise_for_status()

    # The response is an event stream; the JSON message is on the "data:" line
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[len("data:"):])
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]
    raise RuntimeError(f"No data in response: {resp.text[:200]}")


def call_tool(name: str, arguments: dict | None = None) -> dict:
    result = mcp_request("tools/call", {"name": name, "arguments": arguments or {}})
    if result.get("isError"):
        raise RuntimeError(result["content"][0]["text"])
    return result["structuredContent"]


# 1. List the tools
tools = mcp_request("tools/list")["tools"]
print("Tools:", [t["name"] for t in tools])

# 2. List the AI tables
for table in call_tool("list_ai_tables")["tables"]:
    print(f"{table['schema']}.{table['table']}  ->  column: {table['enriched_column']}")

# 3. Read ALL enriched data, page by page
offset = 0
while True:
    page = call_tool("get_enriched_data", {"table": "customer_ai", "limit": 1000, "offset": offset})
    for row in page["rows"]:
        print(row)
    if not page["has_more"]:
        break
    offset += page["row_count"]

# 4. Export the whole column as a CSV file and save it
export = call_tool("export_enriched_data", {"table": "customer_ai", "format": "csv"})
with requests.get(export["download_url"], stream=True, timeout=600) as r:
    r.raise_for_status()
    with open(export["filename"], "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
print("Saved", export["filename"])

# 5. Download a file from S3
link = call_tool("get_download_url", {"key": "reports/q1.csv"})
with requests.get(link["download_url"], timeout=600) as r:
    r.raise_for_status()
    with open("q1.csv", "wb") as f:
        f.write(r.content)
```

---

## 5. Testing with curl

```bash
AF=https://authforge.cfapps.us10-001.hana.ondemand.com
URL=https://s3-mcp-server-terrific-bear-cx.cfapps.us10-001.hana.ondemand.com/mcp

# 1. Get a token
TOKEN=$(curl -s -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials "$AF/api/oauth/token" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
echo "${TOKEN:0:12}…"

# 2. Call the server
curl -X POST "$URL" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 3. Revoke the token when you're done (optional)
curl -u "$CLIENT_ID:$CLIENT_SECRET" -d "token=$TOKEN" "$AF/api/oauth/revoke"
```

---

## 6. Troubleshooting

| You see | Meaning | Fix |
|---|---|---|
| `401` `missing_token` | No `Authorization` header was sent | Get a token and send `Authorization: Bearer at_live_...` |
| `401` `invalid_token_format` | The token doesn't start with `at_live_`, e.g. an old `sk_live_` API key | Use an OAuth access token from `/api/oauth/token` |
| `401` `expired` | The token is more than 1 hour old | Get a new token, or turn on auto-refresh in Postman |
| `401` `revoked` | The token was revoked | Get a new token |
| `401` `quota_exceeded` | The client's usage quota is used up | Ask the AuthForge admin to raise the quota |
| `401` `inactive` | AuthForge doesn't recognise this token | Check for a typo or truncated token, and get a new one |
| Token request returns `401` | The client ID or secret is wrong | Check both values in the AuthForge dashboard |
| `406 Not Acceptable` | The `Accept` header is wrong | Use `Accept: application/json, text/event-stream` |
| `503` `auth_unavailable` | The server can't reach AuthForge | Try again later; contact the server owner if it persists |
| `200` with `"isError": true` | The request worked but the tool failed | Read `result.content[0].text` for the reason |
| Download link gives `403` | The link expired (after 1 hour) or was altered | Call the export tool again for a fresh link |
| `"table_count": 0` | No tables ending in `ai` are visible | Check the table names and the database user's permissions |

Every request is logged on the server, with the reason for each rejection. The server owner can check them with `cf logs s3-mcp-server --recent | grep "auth denied"`.
