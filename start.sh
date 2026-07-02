#!/bin/bash
# 同时启动 evalscope service(:9000) 和 测评台应用(:8000)
set -e

echo "=========================================="
echo " 大模型测评台启动中（基于 evalscope）"
echo "=========================================="

# 离线环境变量：禁止联网下载，强制用本地缓存
export HF_DATASETS_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export MODELSCOPE_OFFLINE=1
export MODELSCOPE_CACHE=${MODELSCOPE_CACHE:-/opt/modelscope_cache}
export EVALSCOPE_URL=${EVALSCOPE_URL:-http://127.0.0.1:9000}

# 1. 后台启动 evalscope service
echo "[1/2] 启动 evalscope service (:9000)..."
evalscope service --host 127.0.0.1 --port 9000 > /app/data/evalscope.log 2>&1 &
ES_PID=$!
echo "      evalscope service PID=$ES_PID，日志：/app/data/evalscope.log"

# 等待 evalscope service 就绪（最多 60 秒）
echo "      等待 evalscope service 就绪..."
for i in $(seq 1 30); do
    if python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9000/health')" 2>/dev/null; then
        echo "      ✓ evalscope service 已就绪"
        break
    fi
    sleep 2
    if [ $i -eq 30 ]; then
        echo "      ⚠ evalscope service 启动超时，请查看 /app/data/evalscope.log"
    fi
done

# 2. 前台启动测评台应用（主进程）
echo "[2/2] 启动测评台应用 (:8000)..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
