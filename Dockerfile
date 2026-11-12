FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY makeros ./makeros
COPY templates ./templates

RUN pip install --no-cache-dir .

# Persistent state lives in the data container's volumes:
#   /data     SQLite DB + backups
#   /private  per-user private notes
# Content (Markdown + uploads) is bind-mounted from the host folder.
ENV MAKEROS_CONTENT_DIR=/app/content \
    MAKEROS_PRIVATE_DIR=/private \
    MAKEROS_TEMPLATES_DIR=/app/templates \
    MAKEROS_DB_PATH=/data/makeros.db

EXPOSE 1313

# --proxy-headers: behind `tailscale serve` the app should see the real
# client address / scheme from X-Forwarded-* headers.
CMD ["uvicorn", "makeros.main:app", "--host", "0.0.0.0", "--port", "1313", "--proxy-headers"]
