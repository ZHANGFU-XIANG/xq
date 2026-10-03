# 前端建置
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# 後端執行（Hugging Face Spaces：使用者 uid 1000、port 7860）
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* && useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
COPY --from=web --chown=user /web/dist ./web/dist
RUN mkdir -p data/cache data/facts data/pdfs reports && chown -R user /app
USER user
ENV APP_MODE=cloud PYTHONIOENCODING=utf-8 REPO_CACHE_DIR=/tmp/invest-repo
EXPOSE 7860
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "7860"]
