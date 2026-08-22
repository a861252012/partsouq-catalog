FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ARG UV_VERSION=0.9.18

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PARTSOUQ_HOME=/app \
    PSQ_CLOAK_PYTHON=/usr/local/bin/python \
    UV_LINK_MODE=copy

# CloakBrowser 0.4.0 提供 Linux x64 / arm64 的指紋修補版 Chromium：
# 需要標準 Chromium 系統依賴，且 headless=False 需要虛擬顯示（Xvfb）
# 才能啟動（xvfb-run 依賴 xauth）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
        libatspi2.0-0 libx11-6 libxcb1 libxext6 libxrender1 libxss1 \
        fonts-liberation xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

# 固定與 macOS host 驗證過相同的版本；binary 在建置時預先下載進
# image（/app/.cloakbrowser），runtime 不再需要外網下載。
ENV CLOAKBROWSER_CACHE_DIR=/app/.cloakbrowser
COPY deploy/requirements-cloakbrowser.txt /tmp/requirements-cloakbrowser.txt
RUN pip install --no-cache-dir --require-hashes \
        -r /tmp/requirements-cloakbrowser.txt \
    && python -m cloakbrowser install \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:${PATH}"
COPY db ./db
COPY migrations ./migrations
COPY deploy/checked-entrypoint.sh /usr/local/bin/partsouq-checked-entrypoint
RUN chmod 0755 /usr/local/bin/partsouq-checked-entrypoint

CMD ["partsouq-admin"]
