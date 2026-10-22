# S3 Explorer MCP Server

A FastMCP server that lists folders and files in an S3 bucket. You can run it locally or deploy it to SAP BTP Cloud Foundry.

## Tools

| Tool | Description |
|------|-------------|
| `list_buckets` | Lists every bucket the credentials can access |
| `list_folders_and_files` | Lists folders and files under an optional `prefix`. Set `recursive` to `true` for every level or `false` for one level only |
| `get_folder_tree` | Returns the same contents as a nested folder tree |
| `get_object_info` | Returns metadata for one object: size, content type, ETag and last-modified time |
| `get_download_url` | Creates a temporary pre-signed link to download a file of any size straight from S3. Valid for 1 hour by default (`expires_in`) |
| `read_file` | Returns a file's contents to the client: text for text files, an embedded file otherwise. Limited to 5 MB (`S3_MAX_READ_BYTES`) |

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
