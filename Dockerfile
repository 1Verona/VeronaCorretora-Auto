# --- estágio 1: build do painel React (frontend/dist não é mais versionado) ---
FROM node:20-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- estágio 2: backend Flask ---
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY . .
COPY --from=frontend /fe/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1
ENV PORT=5050
ENV DB_PATH=/app/data/verona.db
EXPOSE 5050

HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:5050/health || exit 1

CMD ["python", "app.py"]
