# Mosaic Photo-Editor in a container (backup option; the Mac app is recommended).
# AI features are not included: they need the computer's GPU, which Docker on a Mac can't use.
# Build and run from the repository root:  docker compose -f docker/docker-compose.yml up -d --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    MOSAIC_DOCKER=1

RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 1000 mosaic && mkdir -p /data /state && chown mosaic:mosaic /data /state
COPY --chown=mosaic:mosaic app.py ./
COPY --chown=mosaic:mosaic mosaic ./mosaic
COPY --chown=mosaic:mosaic assets ./assets
COPY --chown=mosaic:mosaic .streamlit ./.streamlit
USER mosaic

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
