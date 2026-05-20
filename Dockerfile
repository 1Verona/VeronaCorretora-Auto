FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=5050
EXPOSE 5050

CMD ["python", "app.py"]
