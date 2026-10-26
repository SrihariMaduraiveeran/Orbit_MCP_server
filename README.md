# S3 Explorer MCP Server

A FastMCP server that lists and downloads files in an S3 bucket. It also reads the enriched-data column from PostgreSQL tables whose names end with `ai`. You can run it locally or deploy it to SAP BTP Cloud Foundry.

## S3 tools

## Tools

| Tool | Description |
|------|-------------|
| `list_buckets` | Lists every bucket the credentials can access |
| `list_folders_and_files` | Lists folders and files under an optional `prefix`. Set `recursive` to `true` for every level or `false` for one level only |
| `get_folder_tree` | Returns the same contents as a nested folder tree |
| `get_object_info` | Returns metadata for one object: size, content type, ETag and last-modified time |
| `get_download_url` | Creates a temporary pre-signed link to download a file of any size straight from S3. Valid for 1 hour by default (`expires_in`) |
| `read_file` | Returns a file's contents to the client: text for text files, an embedded file otherwise. Limited to 5 MB (`S3_MAX_READ_BYTES`) |

## PostgreSQL tools

| Tool | Description |
|------|-------------|
| `list_ai_tables` | Lists tables whose names end with `ai`, in any case, and shows which one has the enriched-data column |
| `get_enriched_data` | Reads the enriched column from one table, up to 1000 rows per call, with optional primary-key columns. Use `limit` and `offset` to page through it |
| `export_enriched_data` | Exports the whole column as CSV or JSON. Over HTTP it returns a temporary download link (1 hour by default) served by this server. In local stdio mode it writes the file to `EXPORT_DIR` (default `exports/`) |

Notes on the PostgreSQL tools:

- **Read-only:** every session runs read-only, so the tools can't change data. A database user with `SELECT` access is enough.
- **Column matching:** the column is matched ignoring case, spaces and underscores, so `enriched_data`, `"Enriched Data"` and `EnrichedData` all match. Set `DB_ENRICHED_COLUMN` and `DB_TABLE_SUFFIX` to change what's searched for.
- **Download links:** each link carries a signed token derived from `MCP_API_KEY`, so it works without an `Authorization` header. Anyone with a link can download the file until it expires.
- **Large exports:** the file streams straight from Postgres to the browser, so large exports use little memory.

### On-premise database via Cloud Connector

If PostgreSQL runs in a private network, create a BTP destination for it with these settings:

- **Type:** `TCP`
- **Proxy Type:** `OnPremise`
- **Address:** the Cloud Connector virtual host and port
- **Location ID:** the Cloud Connector's location ID, if one is set

Then set `DB_DESTINATION_NAME` to the destination's name. List a Connectivity instance and a Destination instance under `services:` in `manifest.yml`. Existing instances in the space work; `cf push` only binds them and creates nothing. If none exist, create them with `cf create-service connectivity lite <name>` and `cf create-service destination lite <name>`.

At runtime the server does three things:

1. Reads the address and location ID from the destination.
2. Gets a token from the Connectivity service.
3. Opens a SOCKS5 tunnel through Cloud Connector.

`DB_HOST` and `DB_PORT` aren't used in this mode. `DB_NAME`, `DB_USER` and `DB_PASSWORD` are still required.

Without a Destination service, you can set `DB_PROXY_TYPE=OnPremise`, `DB_HOST`, `DB_PORT` and `DB_LOCATION_ID` directly instead.

If you don't pass `bucket`, every tool uses the configured bucket. On BTP that is the Object Store bucket; locally it is `S3_BUCKET_NAME`.

## Deploy to SAP BTP (Cloud Foundry)

The app gets its AWS credentials and API key from the `env:` section of `manifest.yml`, so it needs no BTP services.

**`manifest.yml` holds real credentials, so git ignores it and it isn't in the repository.** Create it locally from the template below.

| File | Purpose |
|------|---------|
| `manifest.yml` | Defines the CF app and its credentials (local only, git-ignored) |
| `runtime.txt` | Sets the Python version for the buildpack |
| `.cfignore` | Keeps `venv/`, `.env` and the key files out of the upload |

### 1. Log in

```bash
cf login -a https://api.cf.<region>.hana.ondemand.com --sso
cf target -o <org> -s <space>
```

### 2. Create `manifest.yml`

```yaml
---
applications:
  - name: s3-mcp-server
    memory: 256M
    disk_quota: 512M
    instances: 1
    buildpacks:
      - python_buildpack
    command: python s3_server.py
    random-route: true
    health-check-type: http
    health-check-http-endpoint: /health
    env:
      S3_BUCKET_NAME: "<bucket>"
      AWS_REGION: "<region>"
      AWS_ACCESS_KEY_ID: "<access-key-id>"
      AWS_SECRET_ACCESS_KEY: "<secret-access-key>"
      MCP_API_KEY: "<api-key>"   # python3 -c "import secrets; print(secrets.token_urlsafe(32))"
      S3_MAX_ITEMS: "1000"
      DB_HOST: "<db-host>"
      DB_PORT: "5432"
      DB_NAME: "<database-name>"
      DB_USER: "<db-user>"
      DB_PASSWORD: "<db-password>"
      DB_SSLMODE: "require"
```

### 3. Push the app

```bash
cf push
```

The `routes:` line in the `cf push` output gives the app URL. Check it with:

```bash
curl https://<app-route>/health        # {"status":"ok"}
```

The MCP endpoint is `https://<app-route>/mcp`. Every call needs the header `Authorization: Bearer <MCP_API_KEY>`.

### 4. Connect Claude Code to the deployed server

```bash
claude mcp add --transport http s3-explorer https://<app-route>/mcp \
  --header "Authorization: Bearer <MCP_API_KEY>"
```

To change a credential later, edit `manifest.yml` and run `cf push` again.

### Optional: XSUAA instead of an API key

If an XSUAA service is bound, the server validates XSUAA JWTs (scope `<xsappname>.Read`, defined in `xs-security.json`) instead of the API key:

```bash
cf create-service xsuaa application s3-mcp-xsuaa -c xs-security.json
cf bind-service s3-mcp-server s3-mcp-xsuaa && cf restage s3-mcp-server
```

The server also reads S3 credentials from a bound SAP Object Store service (label `objectstore`) when one exists. Those take priority over the environment variables.

### Useful commands

```bash
cf logs s3-mcp-server --recent     # application logs
cf restage s3-mcp-server           # after re-binding services
cf scale s3-mcp-server -i 2        # more instances (the server is stateless)
```

## Authentication (AuthForge)

Every HTTP request must send `Authorization: Bearer sk_live_...`. `authforge.py` checks each key with `POST {AUTHFORGE_URL}/api/auth/verify`:

| Result | Response |
|---|---|
| Valid | The request passes through. `app_id`, `key_name` and `scope` are added to `request.state.authforge` |
| Invalid, revoked, expired or over quota | `401` with AuthForge's reason |
| No header, or key doesn't start with `sk_live_` | `401`, without calling AuthForge |
| AuthForge unreachable or returning 5xx | `503` (fails closed) |

These paths are public: `/health`, `/docs`, `/openapi.json` and `/download/enriched`. Download links carry their own signed, expiring token because browsers can't send headers. Change the list with `AUTHFORGE_PUBLIC_PATHS`.

| Setting | Purpose |
|---|---|
| `AUTHFORGE_URL` | Required; the server refuses to start without it |
| `AUTHFORGE_TIMEOUT_SECONDS` | Verify timeout, default 5 seconds |
| `AUTHFORGE_DISABLED=true` | Local development only |

When AuthForge is enabled it replaces the `MCP_API_KEY` and XSUAA checks. `MCP_API_KEY` is still used to sign download links.

## Run locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in bucket + AWS credentials

python s3_server.py                         # stdio, for Claude Desktop / Claude Code
MCP_TRANSPORT=http python s3_server.py      # HTTP at http://localhost:8000/mcp
fastmcp dev s3_server.py                    # MCP Inspector UI for testing
```

Locally, the server uses no authentication unless `MCP_API_KEY` is set. On Cloud Foundry it refuses to start unless `MCP_API_KEY` is set or an XSUAA service is bound. You can override that with `ALLOW_UNAUTHENTICATED=true`, but don't do it in production.

### Note on FIPS-mode OpenSSL (local only)

If startup fails with `UnsupportedAlgorithm: Digest is not supported for HMAC`, the host's OpenSSL config is forcing FIPS-only algorithms. Run the server with `OPENSSL_CONF=` set to an empty value.
