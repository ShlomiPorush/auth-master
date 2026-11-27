FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY services/requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY services/app ./app
COPY services/static ./static

# The service runs as this user. docker/entrypoint.sh starts as root only to give it
# the data directory, then drops to it (see "Data directory and file ownership" in README.md).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/data_dir.py /usr/local/lib/auth-master/data_dir.py
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && chmod 0644 /usr/local/lib/auth-master/data_dir.py

EXPOSE 8080
# Zero-downtime rollout (docs/deploy.md): before docker rollout stops this container it runs
# the hook, the health check fails while /tmp/drain exists, and the reverse proxy stops
# routing here. A 10 s interval turns a drained container unhealthy within the hook's sleep.
LABEL docker-rollout.pre-stop-hook="touch /tmp/drain && sleep 45"
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
  CMD [ ! -e /tmp/drain ] && python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health').read()"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
