# Caspian 应用镜像（网关 + 沙箱客户端）。
# 说明：镜像只含网关和沙箱 client（agent-sandbox SDK），不含任何 Docker daemon。
#       沙箱 daemon 由独立 sandbox-docker (dind) sidecar 提供，经 DOCKER_HOST 连接。
FROM python:3.11-slim

# apt 镜像源主机（默认 deb.debian.org；网络受限/内网时可构建参数覆盖，例如：
#   docker compose build --build-arg APT_MIRROR_HOST=mirrors.aliyun.com
#）
ARG APT_MIRROR_HOST=deb.debian.org

# 重型文档/OCR 工具（LibreOffice + tesseract + CJK 字体）为**可选**：默认关闭以得到最小、最快启动的镜像。
# 需要 docx/pptx 转换或 OCR 时开启：docker compose build --build-arg INSTALL_DOC_TOOLS=1
# （默认关闭时 app 仍能启动，docx/vision skill 在该工具缺失时于使用阶段报错。）
ARG INSTALL_DOC_TOOLS=0

# 基础系统依赖：仅装轻量、必备的包。重型的 libreoffice/tesseract 由 INSTALL_DOC_TOOLS 控制。
# -o Acquire::Retries=5：apt 下载遇代理抖动（500/EOF/502）自动重试并复用已下载部分。
# 说明：本机代理对明文 HTTP(80) 返回 502，但对 HTTPS 正常，故这里把 apt 源统一切到 HTTPS。
RUN sed -i -E "s|http://(deb\\.debian\\.org)|https://${APT_MIRROR_HOST}|g" \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true \
    && apt-get -o Acquire::Retries=5 update \
    && if [ "$INSTALL_DOC_TOOLS" = "1" ]; then \
         apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
             libreoffice tesseract-ocr fonts-noto-cjk; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制源码，安装 harness 包（editable）使 `import caspian` 在容器内可解析。
# extras: postgres(asyncpg/checkpointer-postgres) + aiosandbox(agent-sandbox/docker)
#         + mcp + dev(fastapi)；另加 uvicorn 供 run_dev.py 使用。
# --retries/--timeout：pip 遇代理抖动自动重试、延长单次请求超时。
# PIP_INDEX_URL：pypi 源，默认 https://pypi.org/simple；网络受限/内网可用
#   --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ 覆盖。
COPY . .
ARG PIP_INDEX_URL=https://pypi.org/simple
# psycopg-binary：langgraph-checkpoint-postgres 用 psycopg，slim 镜像无系统 libpq，
# 需安装自带编译产物的二进制 wheel。
RUN pip install --no-cache-dir --retries 5 --timeout 120 --default-timeout 120 \
        --index-url "$PIP_INDEX_URL" \
        -e "backend/packages/harness[postgres,aiosandbox,mcp,dev]" \
        psycopg-binary \
        uvicorn

# 沙箱数据与技能根目录（与 compose 共享卷路径一致：.caspian 与 skills）
RUN mkdir -p /app/.caspian /app/skills

# 默认沙箱后端为容器沙箱；容器内监听所有网卡（由 sandbox-docker 服务发布 8000）。
ENV CASPIAN_SANDBOX=caspian.community.aio_sandbox.aio_sandbox:AioSandbox \
    CASPIAN_HOST=0.0.0.0

EXPOSE 8000

CMD ["sh", "docker/entrypoint.sh"]
