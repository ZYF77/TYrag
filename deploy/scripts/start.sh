#!/usr/bin/env bash
# ============================================================
# WP-00: Enterprise RAGFlow 一键启动脚本
# 固定版本 v0.26.4，使用 elasticsearch 作为文档引擎。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

COMPOSE_BASE="$PROJECT_ROOT/ragflow/docker/docker-compose.yml"
COMPOSE_ENTERPRISE="$PROJECT_ROOT/deploy/overlays/docker-compose.enterprise.yml"
ENV_FILE="$PROJECT_ROOT/ragflow/docker/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[start.sh]${NC} $*"; }
warn() { echo -e "${YELLOW}[start.sh]${NC} $*"; }
err()  { echo -e "${RED}[start.sh]${NC} $*"; }

check_prereqs() {
    if ! command -v docker &>/dev/null; then
        err "Docker 未安装或不在 PATH 中。"
        exit 1
    fi
    if ! docker compose version &>/dev/null; then
        err "docker compose 不可用（需要 Docker Compose v2）。"
        exit 1
    fi
    if [ ! -f "$COMPOSE_BASE" ]; then
        err "找不到 $COMPOSE_BASE"
        exit 1
    fi
    log "前置检查通过。"
}

init_env() {
    if [ ! -f "$ENV_FILE" ]; then
        warn ".env 不存在，从 .env.example 复制默认配置。"
        cp "$PROJECT_ROOT/ragflow/docker/.env.example" "$ENV_FILE" 2>/dev/null || true
    fi
    if grep -q "RAGFLOW_IMAGE" "$ENV_FILE" 2>/dev/null; then
        sed -i.bak 's/^RAGFLOW_IMAGE=.*/RAGFLOW_IMAGE=tyrag\/ragflow:v0.26.4/' "$ENV_FILE"
        rm -f "$ENV_FILE.bak"
    fi
}

start_ragflow() {
    log "启动 RAGFlow 基础服务（elasticsearch + mysql + minio + redis + ragflow-cpu）..."
    docker compose -f "$COMPOSE_BASE" up -d --wait
    log "RAGFlow 基础服务已启动。"
}

wait_ragflow_ready() {
    local max_attempts=30
    local attempt=1
    log "等待 RAGFlow API 就绪 (http://localhost:9380)..."
    while [ $attempt -le $max_attempts ]; do
        if curl -sf http://localhost:9380/api/v1/system/ping >/dev/null 2>&1; then
            log "RAGFlow API 就绪。（第 ${attempt} 次尝试）"
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    err "RAGFlow API 在 ${max_attempts} 次尝试后仍未就绪。"
    return 1
}

run_enterprise_health() {
    log "执行企业健康检查..."
    docker compose \
        -f "$COMPOSE_BASE" \
        -f "$COMPOSE_ENTERPRISE" \
        --profile enterprise \
        run --rm enterprise-health
    if [ $? -eq 0 ]; then
        log "企业健康检查通过。"
    else
        warn "企业健康检查返回非零，请检查日志。"
    fi
}

stop_all() {
    log "停止所有服务..."
    docker compose -f "$COMPOSE_BASE" down
    log "已停止。"
}

usage() {
    echo "用法: $0 {start|stop|status}"
    echo ""
    echo "  start   启动 RAGFlow + 企业健康检查（默认）"
    echo "  stop    停止所有服务"
    echo "  status  查看服务状态"
    exit 1
}

main() {
    local cmd="${1:-start}"
    case "$cmd" in
        start)
            check_prereqs
            init_env
            start_ragflow
            wait_ragflow_ready
            run_enterprise_health
            log "全部就绪。RAGFlow API: http://localhost:9380"
            ;;
        stop)
            stop_all
            ;;
        status)
            docker compose -f "$COMPOSE_BASE" ps
            ;;
        *)
            usage
            ;;
    esac
}

main "$@"