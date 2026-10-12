#!/bin/sh
# 低 IO 优先级重建并部署公网版（2026-10-12 起：弱 IO 服务器重建卡死复盘）。
#
# 三层减负：
# 1. nice/ionice 压低客户端优先级（上下文打包/上传阶段让出 CPU 与磁盘）
# 2. daemon 侧 IO 靠仓库根 .dockerignore 瘦身上下文 + 层缓存命中
#    （requirements.txt 不变则 pip 层不重跑，日常改动只重放 COPY）
# 3. 运行期日志轮转与 IO 权重见 docker-compose.yml
#
# 用法：server/scripts/deploy.sh   （等价于 cd server && build + up -d）
set -e
cd "$(dirname "$0")/.."
nice -n 19 ionice -c 3 docker-compose build lazy-clerk
docker-compose up -d lazy-clerk
