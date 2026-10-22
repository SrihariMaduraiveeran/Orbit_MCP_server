# S3 Explorer MCP Server

A FastMCP server that lists folders and files in an S3 bucket. You can run it locally or deploy it to SAP BTP Cloud Foundry.

## Tools

| Tool | Description |
|------|-------------|
| `list_buckets` | Lists every bucket the credentials can access |
| `list_folders_and_files` | Lists folders and files under an optional `prefix`. Set `recursive` to `true` for every level or `false` for one level only |
| `get_folder_tree` | Returns the same contents as a nested folder tree |
| `get_object_info` | Returns metadata for one object: size, content type, ETag and last-modified time |

If you don't pass `bucket`, every tool uses the configured bucket. On BTP that is the Object Store bucket; locally it is `S3_BUCKET_NAME`.

## Deploy to SAP BTP (Cloud Foundry)

The app gets its AWS credentials and API key from environment variables set in `manifest.yml`, so it needs no BTP services. The real values live in `vars.yml`, which git ignores; the manifest only holds `((placeholders))`.

| File | Purpose |
|------|---------|
| `manifest.yml` | Defines the CF app: Python buildpack, `/health` health check, and environment variables |
| `vars.example.yml` | Template for `vars.yml`, which holds the real credentials (never committed) |
| `runtime.txt` | Sets the Python version for the buildpack |
| `.cfignore` | Keeps `venv/`, `.env`, `vars.yml` and the key files out of the upload |

### 1. Log in

```bash
cf login -a https://api.cf.<region>.hana.ondemand.com --sso
cf target -o <org> -s <space>
```

### 2. Fill in `vars.yml`

```bash
cp vars.example.yml vars.yml
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # use as mcp_api_key
```

Set the bucket name, region, AWS access key and secret, and the API key.

### 3. Push the app

```bash
cf push --vars-file vars.yml
```

The `routes:` line in the `cf push` output gives the app URL. Check it with:

```bash
curl https://<app-route>/health        # {"status":"ok"}
```

The MCP endpoint is `https://<app-route>/mcp`. Every call needs the header `Authorization: Bearer <mcp_api_key>`.

### 4. Connect Claude Code to the deployed server

```bash
claude mcp add --transport http s3-explorer https://<app-route>/mcp \
  --header "Authorization: Bearer <mcp_api_key>"
```

To change a credential later, edit `vars.yml` and run `cf push --vars-file vars.yml` again.

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
