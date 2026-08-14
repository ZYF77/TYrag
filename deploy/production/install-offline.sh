#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
IMAGE_ARCHIVE="$SCRIPT_DIR/images/tyrag-images.tar"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose v2 is required" >&2
    exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing $ENV_FILE; copy production.env.example to .env and fill it" >&2
    exit 1
fi
if [[ ! -f "$IMAGE_ARCHIVE" ]]; then
    echo "Missing $IMAGE_ARCHIVE" >&2
    exit 1
fi

FILE_SHARE_ROOT="$(awk -F= '/^ENTERPRISE_FILE_SHARE_HOST_ROOT=/{print $2}' "$ENV_FILE" | tr -d '\r')"
STATE_ROOT="$(awk -F= '/^ENTERPRISE_GATEWAY_STATE_HOST_DIR=/{print $2}' "$ENV_FILE" | tr -d '\r')"
if [[ -z "$FILE_SHARE_ROOT" || -z "$STATE_ROOT" ]]; then
    echo "Both host data directories must be configured in .env" >&2
    exit 1
fi
mkdir -p "$FILE_SHARE_ROOT" "$STATE_ROOT"
if [[ "$(id -u)" -eq 0 ]]; then
    chown 10001:10001 "$STATE_ROOT"
fi

docker load --input "$IMAGE_ARCHIVE"

PROFILE_ARGS=()
if [[ "${1:-}" == "--diagnostics" ]]; then
    PROFILE_ARGS+=(--profile diagnostics)
fi

docker compose \
    --env-file "$ENV_FILE" \
    --file "$COMPOSE_FILE" \
    "${PROFILE_ARGS[@]}" \
    config --quiet

docker compose \
    --env-file "$ENV_FILE" \
    --file "$COMPOSE_FILE" \
    "${PROFILE_ARGS[@]}" \
    up --detach --wait --pull never

docker compose \
    --env-file "$ENV_FILE" \
    --file "$COMPOSE_FILE" \
    "${PROFILE_ARGS[@]}" \
    ps
