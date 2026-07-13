#!/bin/bash
# ============================================================
# 离线打包脚本 - 在【联网机器】上运行
# 把镜像（含 evalscope+依赖+数据集）打包成 tar.gz，拷进内网 load
# ============================================================
set -e

IMAGE_NAME="llm-eval-bench"
IMAGE_TAG="evalscope"
OUTPUT="llm-eval-bench-image.tar.gz"

echo "=========================================="
echo " 步骤 1/3：构建镜像（联网，会下载 evalscope+数据集）"
echo "=========================================="
echo " 这一步耗时较长（下载依赖和数据集），请耐心等待..."
docker build -t ${IMAGE_NAME}:${IMAGE_TAG} .

echo ""
echo "=========================================="
echo " 步骤 2/3：导出镜像为 tar.gz"
echo "=========================================="
docker save ${IMAGE_NAME}:${IMAGE_TAG} | gzip > ${OUTPUT}
SIZE=$(du -h ${OUTPUT} | cut -f1)
echo " ✓ 已导出：${OUTPUT}（${SIZE}）"

echo ""
echo "=========================================="
echo " 步骤 3/3：完成。接下来在内网操作："
echo "=========================================="
echo " 1. 把以下文件拷进内网："
echo "      - ${OUTPUT}        （镜像）"
echo "      - docker-compose.yml （编排文件）"
echo " 2. 在内网机器执行："
echo "      docker load < ${OUTPUT}"
echo "      docker compose up -d"
echo " 3. 浏览器访问 http://<内网机器IP>:8000"
echo "=========================================="
