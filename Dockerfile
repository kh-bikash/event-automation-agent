FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home app && mkdir /data && chown app:app /data
USER app
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 APP_DB=/data/events.sqlite3
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"
CMD ["python", "-m", "event_automation_agent"]
