#!/bin/bash
# tw-archiver 启动脚本
# 用法: ./start.sh [restart|stop|status]

PORT=5010
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/tw-archiver.pid"

case "${1:-start}" in
  start)
    echo "▶ 启动 tw-archiver (port $PORT)..."
    cd "$APP_DIR"
    nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --log-level info > /tmp/tw-archiver.log 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/ 2>/dev/null | grep -q 200; then
      echo "✅ tw-archiver 已启动 (PID $(cat $PID_FILE))"
      echo "   内网: http://localhost:$PORT"
      echo "   外网: http://${PORT}.a.kplkpl.top:7002"
    else
      echo "⚠️  启动可能失败，请查看日志: cat /tmp/tw-archiver.log"
    fi
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      kill "$pid" 2>/dev/null && echo "✅ 已停止 (PID $pid)" || echo "⚠️  进程不存在"
      rm -f "$PID_FILE"
    else
      pkill -f "uvicorn app.main:app" 2>/dev/null && echo "✅ 已停止" || echo "⚠️  未找到运行中的进程"
    fi
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "✅ 运行中 (PID $(cat $PID_FILE))"
      curl -s -o /dev/null -w "   HTTP状态: %{http_code}\n" http://localhost:$PORT/
    else
      echo "❌ 未运行"
    fi
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
