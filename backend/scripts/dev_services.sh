#!/usr/bin/env bash
# 开发用的三个服务统一管理：前端(3000) / 后端(8000) / 本地假端点(8123)
#
# 为什么需要它：这三个服务是独立进程，改了代码之后**必须重启对应的那个**才会生效。
# 我在这上面栽过三次（改了 mock 却没重启，于是后端连的还是旧剧本，表现为
# 「功能莫名其妙不生效」）。所以统一用这个脚本起停，并在启动时打印它加载的是哪份代码。
#
#   ./scripts/dev_services.sh start     # 启动全部（已在跑的先停掉）
#   ./scripts/dev_services.sh stop      # 停掉全部
#   ./scripts/dev_services.sh restart   # 重启全部
#   ./scripts/dev_services.sh status    # 看谁在跑
#   ./scripts/dev_services.sh logs mock # 看日志
#   ./scripts/dev_services.sh clean [N] # 清理旧 run 目录（默认保留最近 10 个）
set -u

BACKEND_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="$(cd "$BACKEND_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"
LOG_DIR="$BACKEND_DIR/.dev-logs"
mkdir -p "$LOG_DIR"

PY="$BACKEND_DIR/.venv/bin/python"

# 前端工具的缓存目录要留在工作区内（这台机器的家目录是只读的）
export XDG_CACHE_HOME="$PROJECT_DIR/.cache"
export XDG_CONFIG_HOME="$PROJECT_DIR/.config"
export XDG_DATA_HOME="$PROJECT_DIR/.local/share"
export npm_config_store_dir="$PROJECT_DIR/.pnpm-store"
export NEXT_TELEMETRY_DISABLED=1

# 端口是否空闲（直接试绑定，比 curl 可靠：curl 到 200 可能是旧进程给的）
port_free() {
  "$PY" -c "
import socket, sys
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(('127.0.0.1', int(sys.argv[1])))
    print('free')
except OSError:
    print('busy')
finally:
    sock.close()
" "$1"
}

require_free() {
  local port=$1 name=$2
  if [ "$(port_free "$port")" = "busy" ]; then
    echo "  ❌ 端口 $port 已被占用，$name 起不来。" >&2
    echo "     先停掉占用的进程（./scripts/dev_services.sh stop），" >&2
    echo "     如果它不是你起的，用 ss -ltnp | grep $port 找 PID 再 kill。" >&2
    echo "     否则新进程会静默绑定失败，而你访问到的仍是旧代码的服务。" >&2
    return 1
  fi
  return 0
}

stop_one() {
  local pattern=$1 name=$2
  # 方括号小技巧：避免 pkill 把自己的命令行也匹配上（那会把当前 shell 杀掉）
  if pkill -f "$pattern" 2>/dev/null; then
    echo "  已停止 $name"
    sleep 1
  else
    echo "  $name 本来就没在跑"
  fi
}

start_backend() {
  (cd "$BACKEND_DIR" && PYTHONPATH="$BACKEND_DIR" PAPERLENS_ALLOW_LOCAL_REPO_PATHS=true \
    nohup "$PY" -m uvicorn app.main:app --port 8000 --log-level warning \
    > "$LOG_DIR/backend.log" 2>&1 &)
  echo "  后端     : http://127.0.0.1:8000  （本地仓库开关已打开，日志 .dev-logs/backend.log）"
}

start_mock() {
  (cd "$BACKEND_DIR" && PYTHONPATH="$BACKEND_DIR" \
    nohup "$PY" -m uvicorn devtools.mock_provider:app --port 8123 --log-level warning \
    > "$LOG_DIR/mock.log" 2>&1 &)
  echo "  假端点   : http://127.0.0.1:8123  （日志 .dev-logs/mock.log）"
}

start_frontend() {
  (cd "$FRONTEND_DIR" && nohup pnpm dev > "$LOG_DIR/frontend.log" 2>&1 &)
  echo "  前端     : http://localhost:3000  （日志 .dev-logs/frontend.log）"
}

wait_ready() {
  local url=$1 label=$2
  for _ in $(seq 1 40); do
    if curl -s -m 2 -o /dev/null "$url"; then
      echo "  ✅ $label 就绪"
      return 0
    fi
    sleep 0.5
  done
  echo "  ⚠️  $label 没在 20 秒内就绪，看日志：.dev-logs/"
  return 1
}

case "${1:-status}" in
  start|restart)
    echo "停止旧进程…"
    stop_one "[u]vicorn app.main:app" "后端"
    stop_one "[u]vicorn devtools.mock_provider:app" "假端点"
    stop_one "[n]ext dev" "前端"
    echo "启动新进程…"
    failed=0
    require_free 8000 "后端" || failed=1
    require_free 8123 "假端点" || failed=1
    require_free 3000 "前端" || failed=1
    if [ "$failed" -ne 0 ]; then
      echo "启动中止：端口没清干净。" >&2
      exit 1
    fi
    start_backend
    start_mock
    start_frontend
    echo "等待就绪…"
    wait_ready "http://127.0.0.1:8000/api/health" "后端"
    wait_ready "http://127.0.0.1:8123/health" "假端点"
    wait_ready "http://127.0.0.1:3000/" "前端"
    echo
    echo "打开 http://localhost:3000 ，provider 填："
    echo "  base_url = http://127.0.0.1:8123/v1   api_key = mock-key   model = mock-model"
    ;;
  stop)
    stop_one "[u]vicorn app.main:app" "后端"
    stop_one "[u]vicorn devtools.mock_provider:app" "假端点"
    stop_one "[n]ext dev" "前端"
    ;;
  status)
    # 用每个服务**自己的健康检查端点**探测，并且 curl -sf（HTTP 4xx/5xx 也算失败）。
    # 之前只 curl "/" 就报"✅ 在跑"：端口被别的程序占用时（真实发生过：8123 被一个
    # 无关的 http.server 占着），状态照样显示绿勾，人会照着它去连一个冒名的服务。
    status_one() {
      local port=$1 name=$2 health=$3
      if [ "$(port_free "$port")" = "busy" ]; then
        if curl -sf -m 2 -o /dev/null "$health"; then
          echo "  ✅ $name（端口 $port）在跑，健康检查通过（$health）"
        else
          echo "  ⚠️  端口 $port 被占用，但 $name 的健康检查（$health）没通过——"
          echo "     大概率是别的程序占了这个端口。用 ss -ltnp | grep $port 看 PID。"
        fi
      else
        echo "  ❌ $name（端口 $port）没跑"
      fi
    }
    status_one 8000 "后端" "http://127.0.0.1:8000/api/health"
    status_one 8123 "假端点" "http://127.0.0.1:8123/health"
    status_one 3000 "前端" "http://127.0.0.1:3000/"
    ;;
  clean)
    # 清理旧的 run 目录。克隆/事件/产物都是可再生的派生数据（克隆会按需重建，
    # 且 data/repo-cache/ 缓存不在此列），只有 meta/plan/artifact 是分析记录——
    # 所以默认保留最近 10 个 run，更老的才删。
    # 用法: ./scripts/dev_services.sh clean [保留个数]
    keep=${2:-10}
    data_dir="$BACKEND_DIR/data"
    if [ ! -d "$data_dir" ]; then
      echo "  data/ 不存在，没东西可清"
      exit 0
    fi
    removed=0
    # repo-cache 是克隆缓存（正向收益），明确排除；按修改时间 newest-first 排序
    while IFS= read -r dir; do
      echo "  删除 $(basename "$dir")"
      rm -rf "$dir"
      removed=$((removed + 1))
    done < <(find "$data_dir" -mindepth 1 -maxdepth 1 -type d ! -name "repo-cache" -print0 \
      | xargs -0 -r stat -c "%Y	%n" | sort -rn | tail -n +$((keep + 1)) | cut -f2-)
    echo "  已删除 $removed 个旧 run 目录（保留最近 $keep 个）；克隆缓存 repo-cache 未动。"
    ;;

  logs)
    name=${2:-backend}
    tail -n 40 "$LOG_DIR/$name.log"
    ;;
  *)
    echo "用法：$0 {start|stop|restart|status|clean [N]|logs [backend|mock|frontend]}"
    exit 1
    ;;
esac
