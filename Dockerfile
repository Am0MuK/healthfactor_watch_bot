FROM python:3.12-slim

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install project
COPY pyproject.toml README.md /app/
COPY hfwb/ /app/hfwb/
RUN pip install --no-cache-dir .

# Create service directory and ensure permissions
RUN mkdir -p /data/services/healthfactor_watch_bot && \
    chown -R appuser:appuser /data/services/healthfactor_watch_bot /app

USER appuser

# Healthcheck checking heartbeat file timestamp is less than 900 seconds old (3 poll cycles)
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c 'import time, sys, os; f="/data/services/healthfactor_watch_bot/heartbeat"; sys.exit(0 if os.path.exists(f) and (time.time() - int(open(f).read().strip())) < 900 else 1)'

ENTRYPOINT ["hfwb"]
