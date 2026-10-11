FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home bot
COPY --chown=10001:10001 statezero ./statezero
USER 10001:10001
ENV BOT_CONFIG=/app/config.json BOT_DATA_DIR=/app/data
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 CMD ["python", "-m", "statezero", "--health-check"]
CMD ["python", "-m", "statezero"]
