FROM python:3.12-slim
# The newest format of stored data this image reads (deploy/deploy.sh refuses to
# start an image below what the database and Redis already hold).
LABEL org.converge.data-format="2"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Local OCR only: Portuguese/English recognition and bounded PDF rendering.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
       tesseract-ocr-eng poppler-utils ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY cli/ ./cli/
# Only the migrations: init.sql is the baseline Postgres runs itself.
COPY infra/migrations/ ./infra/migrations/

# Fixed ids: the media folder on the server and the OCR socket volume
# (deploy/docker-compose.prod.yml) are owned by 100:101.
RUN addgroup --system --gid 101 app && adduser --system --uid 100 --ingroup app app \
    && mkdir -p /data/media /run/ocr \
    && chown -R app:app /data /run/ocr
USER app

EXPOSE 8000

CMD ["uvicorn", "core.web.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
