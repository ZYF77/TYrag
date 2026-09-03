# TYRAG Production Pilot Bundle

This directory defines a single-Gateway production-server pilot. It is not a
high-availability deployment and does not include production data or secrets.

## Build on an online machine

Build the Gateway image from the exact RAGFlow image that will be shipped:

```bash
docker build \
  --build-context contracts=./contracts \
  --build-arg GATEWAY_BASE_IMAGE=tyrag/ragflow:v0.26.4 \
  -f enterprise/gateway/Dockerfile \
  -t tyrag/enterprise-gateway:v0.26.4 \
  enterprise
```

Build the optional diagnostics UI image:

```bash
docker build \
  --build-arg VITE_API_MODE=gateway \
  --build-arg VITE_UI_MODE=harness \
  -f enterprise/web/Dockerfile \
  -t tyrag/enterprise-web:v0.26.4 \
  enterprise/web
```

Generate the image archive and checksums from PowerShell:

```powershell
pwsh ./deploy/production/package-offline.ps1 -IncludeDiagnostics
```

Without `-IncludeDiagnostics`, the test frontend image is not included.

## Install on the offline server

Copy the generated release directory to the server. Do not copy the local
development `.env`.

```bash
cp production.env.example .env
chmod 600 .env
chmod +x install-offline.sh
# Edit .env with production values.
./install-offline.sh
```

To run the diagnostics UI as an internal-only service:

```bash
./install-offline.sh --diagnostics
```

The installer loads the archive, validates Compose without pulling, and starts
the selected profile with `--pull never`.

## Local WebUI operator login

The Console and Harness share one Gateway session; the only local username is
`zkadmin`. Before enabling the
diagnostics profile, generate a password hash on an offline administration
machine and place only that hash, a random session secret, and the single EAM
tenant id in the protected production environment file:

```bash
python -m enterprise.scripts.generate_console_password_hash
```

Set the resulting value as `ENTERPRISE_CONSOLE_PASSWORD_HASH` and keep the
plaintext password out of the repository, image, logs, and environment file.
Set `ENTERPRISE_CONSOLE_COOKIE_SECURE=true` when HTTPS terminates in front of
the WebUI; it remains false for the current HTTP-only local pilot. Changing
the hash, session secret, or tenant id requires recreating only the Gateway
(rotate the session secret with the password hash to invalidate existing
sessions);
the WebUI image changes only when its frontend code changes. RAGFlow is not
changed by this login.

The Console system-settings page persists Gateway worker switches, polling
intervals, attachment TTL, quality timeout, file limits, and RAG diagnostics
in the `gateway_runtime_settings` row. These Gateway-owned values are editable
and hot-reload on save. RAGFlow processing values (`MAX_CONCURRENT_TASKS`,
`MAX_CONCURRENT_CHUNK_BUILDERS`, and `WORKERS`) remain read-only projections;
changing them requires restarting `ragflow-cpu`.

## HMAC Credential Handoff

`ENTERPRISE_SYNC_HMAC_CREDENTIALS` is a server-side JSON configuration value.
Create one credential per calling system and bind it to the exact
`tenantId`/`sourceSystem` pair. Generate and store the secret in the approved
secret manager, inject the same value into the Gateway process and the trusted
device-system producer, and deliver it to the other party out of band. There
is intentionally no API that returns an HMAC secret.

The device system only needs the Gateway URL, `keyId`, the agreed binding, and
the secret from that secure handoff. It signs every v3 request with
HMAC-SHA256; the Gateway recomputes and verifies the signature. Do not put the
secret in this repository, a request body, a Postman collection, or a browser
environment.

For the controlled development-style server pilot only, add the test override
and the server-side development environment files. This override enables the
explicit HS256 test path and must not be used for formal production traffic:

```bash
docker compose \
  --env-file production.env.example \
  --env-file dev-ragflow.env \
  --env-file dev-enterprise.env \
  --env-file core.env \
  --env-file gateway-overrides.env \
  --env-file gateway-key.env \
  -f docker-compose.yml \
  -f docker-compose.test.yml \
  --profile diagnostics up -d --wait --pull never
```

## Required external dependencies

The following are not included in the image archive and must be reachable from
the server:

- Customer SSO/JWKS endpoint;
- Read-only FILE_SHARE host directory. When EAM is on another server, the
  EAM source directory must be mounted on the Gateway host first; Compose only
  bind-mounts the local host path into the container and cannot mount a remote
  EAM path by itself;
- LLM and embedding endpoints or locally supplied model services;
- Business PostgreSQL only if a separately accepted query adapter is enabled.

An EAM-owned read-only asset resolver can be configured for a later query or
canonical-asset validation flow. It is not required for FILE_SHARE v3 document
ingestion and is not a separate Asset Registry service in this bundle.

## Data and rollback

The archive does not contain MySQL, Elasticsearch, MinIO, or Redis. Gateway
state is stored in the dedicated `gateway-postgres` Compose service and its
`gateway_postgres_data` volume; this bundle intentionally runs one Gateway
instance only. Before an upgrade, back up that volume and, when migrating an
existing pilot, keep a read-only backup of the old SQLite file until the
manifest-verified cutover is complete.

For a one-time SQLite-to-PostgreSQL cutover, stop the Gateway, start only
`gateway-postgres`, and run
`enterprise/scripts/migrate_gateway_sqlite_to_postgres.py` with the old SQLite
file as its read-only source. The target must be empty. Recreate Gateway only
after the per-table row counts and SHA-256 manifest are verified. The runtime
image contains `asyncpg`; it does not use SQLite.

Do not expose MySQL, Elasticsearch, MinIO, or Redis ports to the network. Put
TLS and a reverse proxy in front of the Gateway before changing its bind
address from loopback.
