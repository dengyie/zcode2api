# zcode-hub 容器镜像：Python 3.11 网关 + Node 22 验证码求解子进程 + Debian Chromium
#
# 运行时结构：
#   /app            代码（app/ cli.py frontend/ captcha_node/）
#   /app/data       持久化（accounts.db + device_mid），挂载卷
#   /usr/bin/node   从 node:22 官方镜像复制（bookworm 与运行时基础镜像同系，glibc 兼容）
#   /usr/bin/chromium  Debian chromium，solver_pw.js findChromium() 默认探测路径之一
#
# captcha 求解依赖真浏览器（solver 已带 --no-sandbox/--disable-dev-shm-usage），
# 且 oom 看门狗需要写 /proc/*/oom_score_adj —— 容器以 root 运行是前提，勿改 USER。

# ── 阶段 1：求解器 Node 依赖（纯 JS，无原生编译） ─────────────────────────────
FROM node:22-bookworm-slim AS captcha-deps
WORKDIR /build
COPY captcha_node/package.json captcha_node/package-lock.json ./
RUN npm ci --omit=dev

# ── 阶段 2：运行时 ────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ZCODE_HOST=0.0.0.0 \
    ZCODE_PORT=3000 \
    ZCODE_DATA_DIR=/app/data \
    ZCODE_NODE_PATH=/usr/local/bin/node \
    ZCODE_CHROMIUM_PATH=/usr/bin/chromium

# tini 作 PID 1：回收 uvicorn→node→chrome 进程树遗留的僵尸
# fonts-noto-cjk：验证码挑战页含中文文案，缺字形可能影响求解
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        fonts-liberation \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Node 运行时（构建期不需要 npm，只带 node 本体）
COPY --from=captcha-deps /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

# 运行时依赖（与 requirements.txt 上半段保持一致；dev/test 工具不进镜像）
RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.30.0" \
    "httpx>=0.27.0" \
    "python-dotenv>=1.0.1" \
    "cryptography>=42.0.0" \
    "pyyaml>=6.0.0"

COPY app/ app/
COPY captcha_node/ captcha_node/
COPY --from=captcha-deps /build/node_modules/ captcha_node/node_modules/
COPY frontend/ frontend/
COPY cli.py ./

# data/ 不打进镜像（.dockerignore 双保险），挂卷持久化
RUN mkdir -p /app/data

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "cli.py", "serve"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:3000/meta', timeout=4)"]
