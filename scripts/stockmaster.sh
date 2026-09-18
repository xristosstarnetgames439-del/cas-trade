#!/usr/bin/env bash
# StockMaster 本地开发启动器
# 用法: scripts/stockmaster.sh [start|stop|status]
# 默认 start：停掉 Docker 容器 -> 首次自动装依赖 -> 同时启动后端(--reload)与前端(Vite HMR)
#             -> 打开浏览器。Ctrl+C 或关闭本窗口即可同时停止前后端。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_PORT="${STRONG_STOCK_WEB_PORT:-3110}"
API_PORT="${STRONG_STOCK_API_PORT:-8010}"
AUTH_URL="http://localhost:${WEB_PORT}/auction"
API_URL="http://127.0.0.1:${API_PORT}"

export PATH="$HOME/.local/bin:$PATH"

info() { printf "\\033[1;32m[StockMaster]\\033[0m %s\\n" "$*"; }
warn() { printf "\\033[1;33m[StockMaster]\\033[0m %s\\n" "$*"; }

resolve_uv() { command -v uv 2>/dev/null || true; }

resolve_pnpm() {
  if command -v pnpm >/dev/null 2>&1; then printf "pnpm"; return 0; fi
  if command -v npx >/dev/null 2>&1; then printf "npx --yes pnpm@9.15.0"; return 0; fi
  if command -v corepack >/dev/null 2>&1; then printf "corepack pnpm"; return 0; fi
  return 1
}

port_pid() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

free_port() {
  local pid
  pid="$(port_pid "$1")"
  if [ -n "$pid" ]; then
    warn "端口 $1 被进程 $pid 占用，正在停止..."
    kill "$pid" 2>/dev/null || true
    sleep 1
    pid="$(port_pid "$1")"
    if [ -n "$pid" ]; then kill -9 "$pid" 2>/dev/null || true; sleep 1; fi
  fi
}

stop_docker() {
  if command -v docker >/dev/null 2>&1 && docker ps -q -f name=strong-stock-screener 2>/dev/null | grep -q .; then
    info "停止 Docker 容器 strong-stock-screener（释放端口，数据在 ./data 卷里不会丢）..."
    docker stop strong-stock-screener >/dev/null 2>&1 || true
  fi
}

wait_http() {
  local url="$1" timeout="${2:-90}" attempt=1
  while [ "$attempt" -le "$timeout" ]; do
    if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null; then return 0; fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

start() {
  local uv pnpm_cmd
  uv="$(resolve_uv)"
  if [ -z "$uv" ]; then
    warn "未找到 uv，请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi
  pnpm_cmd="$(resolve_pnpm)" || {
    warn "未找到 pnpm/corepack/npx，请先安装 Node.js"
    pnpm_cmd=""
  }

  stop_docker
  free_port "$WEB_PORT"
  free_port "$API_PORT"

  if [ -n "$uv" ] && [ ! -d "$ROOT/apps/api/.venv" ]; then
    if [ ! -f "$ROOT/apps/api/vendor/eltdx/pyproject.toml" ]; then
      info "首次运行：克隆通达信协议库到 apps/api/vendor/eltdx（不提交 git）..."
      bash "$ROOT/apps/api/scripts/fetch-eltdx.sh" || warn "eltdx 克隆失败，uv sync 可能无法安装竞价依赖"
    fi
    info "首次运行：安装后端依赖（uv sync，约几分钟）..."
    (cd "$ROOT/apps/api" && "$uv" sync) || warn "后端依赖安装失败，稍后 uv run 会重试"
  fi
  if [ -n "$pnpm_cmd" ] && [ ! -d "$ROOT/apps/web-vue/node_modules" ]; then
    info "首次运行：安装前端依赖（pnpm install）..."
    (cd "$ROOT/apps/web-vue" && $pnpm_cmd install --frozen-lockfile) || warn "前端依赖安装失败"
  fi

  if [ -z "$uv" ] || [ -z "$pnpm_cmd" ]; then
    warn "缺少运行环境，已中止。"
    return 1
  fi

  info "启动后端 API（${API_PORT}，--reload）..."
  (
    cd "$ROOT/apps/api"
    export STRONG_STOCK_DATA_DIR="$ROOT/data"
    "$uv" run uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --reload
  ) &
  BPID=$!

  info "启动前端（${WEB_PORT}，Vite HMR）..."
  (
    cd "$ROOT/apps/web-vue"
    $pnpm_cmd dev --host 127.0.0.1 --port "$WEB_PORT"
  ) &
  FPID=$!

  trap 'info "停止 StockMaster..."; kill $BPID $FPID 2>/dev/null || true; pkill -f "uvicorn app.main:app" 2>/dev/null || true; pkill -f "vite --mode test" 2>/dev/null || true' EXIT INT TERM

  info "等待服务就绪..."
  wait_http "$API_URL/health" 120 || warn "后端未就绪，可能仍在安装依赖/启动中"
  wait_http "$AUTH_URL" 120 || warn "前端未就绪，请查看上方 Vite 输出"
  info "已就绪，打开 ${AUTH_URL}"
  open "$AUTH_URL" 2>/dev/null || true
  info "按 Ctrl+C 或关闭本窗口即可同时停止前后端。"

  wait "$BPID" "$FPID" 2>/dev/null || true
}

stop() {
  info "停止 StockMaster..."
  local port pid
  for port in "$WEB_PORT" "$API_PORT"; do
    pid="$(port_pid "$port")"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  pkill -f "vite --mode test" 2>/dev/null || true
  sleep 1
  info "已停止。"
}

status() {
  local api_pid web_pid
  api_pid="$(port_pid "$API_PORT")"
  web_pid="$(port_pid "$WEB_PORT")"
  if [ -n "$api_pid" ]; then info "后端 API(${API_PORT}): 运行中 (pid $api_pid)"; else info "后端 API(${API_PORT}): 未运行"; fi
  if [ -n "$web_pid" ]; then info "前端 Web(${WEB_PORT}): 运行中 (pid $web_pid)"; else info "前端 Web(${WEB_PORT}): 未运行"; fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "用法: $0 [start|stop|status]" >&2; exit 1 ;;
esac
