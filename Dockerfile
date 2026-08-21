FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PARTSOUQ_HOME=/app

# CloakBrowser 是 Linux x64 的指紋修補版 Chromium：需要標準 Chromium
# 系統依賴，且 headless=False 需要虛擬顯示（Xvfb）才能啟動。
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
RUN pip install --no-cache-dir "cloakbrowser==0.4.0" \
    && python -m cloakbrowser install

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY db ./db

CMD ["partsouq-admin"]
