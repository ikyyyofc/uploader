# Base image: Node.js 22 LTS on Debian Bookworm Slim (includes Python 3.11 for Headroom AI)
FROM node:22-bookworm-slim

# Environment configuration for Koyeb & Headroom AI
ENV NODE_ENV=production \
    PORT=7860 \
    HEADROOM_PORT=8787 \
    HEADROOM_BASE_URL=http://127.0.0.1:8787 \
    HEADROOM_OFFLINE=1 \
    HEADROOM_DETECT_BACKEND=python \
    HEADROOM_TIKTOKEN_LOAD_TIMEOUT_SECONDS=0.05 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \
    HEADROOM_WORKSPACE_DIR=/tmp/.headroom \
    DO_NOT_TRACK=1 \
    HEADROOM_BEACON=off \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/headroom-venv/bin:${PATH}"

# 1. Install Python 3 & create virtual environment for Headroom AI
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      python3-pip \
      curl \
      ca-certificates \
    && python3 -m venv /opt/headroom-venv \
    && /opt/headroom-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/headroom-venv/bin/pip install --no-cache-dir \
      "headroom-ai>=0.44.0" \
      "fastapi>=0.100.0" \
      "uvicorn>=0.23.0" \
      "tree-sitter-language-pack" \
    && mkdir -p /opt/tiktoken-cache /tmp/.headroom \
    && (curl -fsSL --max-time 20 "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" \
        -o "/opt/tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4" || rm -f "/opt/tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4") \
    && (curl -fsSL --max-time 20 "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken" \
        -o "/opt/tiktoken-cache/fb374d419588a4632f3f557e76b4b70aebbca790" || rm -f "/opt/tiktoken-cache/fb374d419588a4632f3f557e76b4b70aebbca790") \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Set the working directory in the container
WORKDIR /usr/src/app

# 3. Copy package.json and package-lock.json and install Node.js dependencies
COPY package*.json ./
RUN npm install --omit=dev && npm cache clean --force

# 4. Copy application source files
COPY . .

# 5. Ensure uploads & Headroom workspace directories are writable by any Koyeb runtime user
RUN mkdir -p /usr/src/app/uploads /tmp/.headroom \
    && chmod -R 777 /usr/src/app/uploads /tmp/.headroom

# 6. Expose default Koyeb / Web Service port
EXPOSE 7860

# 7. Container healthcheck for Koyeb
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-7860}/health || exit 1

# 8. Start Express server (auto-starts headroom_service.py in background)
CMD ["node", "app.js"]
