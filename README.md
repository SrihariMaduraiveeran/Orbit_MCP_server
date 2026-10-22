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

| File | Purpose |
|------|---------|
| `manifest.yml` | Defines the CF app: Python buildpack, `/health` health check, and the bound services |
| `xs-security.json` | Defines the XSUAA `Read` scope that callers need |
| `runtime.txt` | Sets the Python version for the buildpack |
| `.cfignore` | Keeps `venv/`, `.env` and the key files out of the upload |

### 1. Log in

```bash
cf login -a https://api.cf.<region>.hana.ondemand.com
cf target -o <org> -s <space>
```

### 2. Create the services

The service names must match the ones in `manifest.yml`:

```bash
# S3 bucket: SAP Object Store on an AWS-based subaccount
cf create-service objectstore s3-standard s3-mcp-objectstore

# Authentication
cf create-service xsuaa application s3-mcp-xsuaa -c xs-security.json
```

To use an existing AWS S3 bucket instead of SAP Object Store, create a user-provided service with the same name:

```bash
cf create-user-provided-service s3-mcp-objectstore -t objectstore -p '{
  "access_key_id": "...", "secret_access_key": "...",
  "bucket": "my-bucket", "region": "eu-central-1"
}'
```

### 3. Push the app

```bash
cf push
```

The `routes:` line in the `cf push` output gives the app URL. Check it with:

```bash
curl https://<app-route>/health        # {"status":"ok"}
```

The MCP endpoint is `https://<app-route>/mcp`.

### 4. Get an access token

Every call to `/mcp` needs an XSUAA bearer token with the `Read` scope:

```bash
cf create-service-key s3-mcp-xsuaa s3-mcp-key
cf service-key s3-mcp-xsuaa s3-mcp-key      # note url, clientid, clientsecret

curl -s -u '<clientid>:<clientsecret>' \
  '<url>/oauth/token?grant_type=client_credentials' | jq -r .access_token
```

Tokens expire, usually after 12 hours, so fetch a new one when calls start returning `401`.

### 5. Connect Claude Code to the deployed server

```bash
claude mcp add --transport http s3-explorer https://<app-route>/mcp \
  --header "Authorization: Bearer <access_token>"
```

Other BTP apps, such as a Joule agent or an app behind the approuter, can call the endpoint with their own XSUAA token. The token must carry the `s3-mcp-server.Read` scope. Assign the `S3Viewer` role template to users through a role collection.

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

Locally, the server uses no authentication. On Cloud Foundry it refuses to start unless an XSUAA service is bound. You can override that with `ALLOW_UNAUTHENTICATED=true`, but don't do it in production.

### Note on FIPS-mode OpenSSL (local only)

If startup fails with `UnsupportedAlgorithm: Digest is not supported for HMAC`, the host's OpenSSL config is forcing FIPS-only algorithms. Run the server with `OPENSSL_CONF=` set to an empty value.
