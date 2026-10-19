FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml constraints-security.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints-security.txt -e ".[infrastructure,database]" \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/indexes \
    && chown -R appuser:appuser /app
COPY skills ./skills
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=3)"
CMD ["python", "-m", "uvicorn", "deepsearch_agent.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--log-level", "info", "--log-config", "/app/src/deepsearch_agent/logging.json"]
