#!/bin/bash
# 双击启动 Priori —— 已在跑就直接开浏览器，没跑就起服务再开。
# 关掉这个终端窗口 = 关掉服务。
# 无写死路径：项目目录 = 本脚本所在目录；uv 从 PATH 查找。

PORT=8000
URL="http://localhost:${PORT}"

# 本脚本所在目录（即项目根），双击时也能正确定位
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$DIR" || { echo "找不到项目目录 $DIR"; exit 1; }

# 找 uv（PATH 里没有就试常见安装位置）
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
[ -z "$UV" ] && { echo "找不到 uv，请先安装：https://docs.astral.sh/uv/"; exit 1; }

# 已经在跑？直接开浏览器，不再起第二份。
if curl -s -o /dev/null "$URL"; then
  echo "Priori 已在运行，打开浏览器…"
  open "$URL"
  exit 0
fi

echo "启动 Priori… (关闭本窗口即停止服务)"
"$UV" run uvicorn app.main:app --port "$PORT" &
SERVER_PID=$!

# 等端口就绪（最多 ~30s）再开浏览器
for i in $(seq 1 60); do
  if curl -s -o /dev/null "$URL"; then
    echo "就绪，打开浏览器 → $URL"
    open "$URL"
    break
  fi
  sleep 0.5
done

# 前台等着服务进程，让终端窗口保持打开；关窗口就停服务
wait $SERVER_PID
