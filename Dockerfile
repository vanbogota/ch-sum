FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY ghostwriter ./ghostwriter
RUN pip install . && useradd --create-home --uid 1000 app && mkdir -p /app/data /app/secrets && chown -R app /app

COPY persona/escalation.toml persona/*.example.md ./persona/
USER app
EXPOSE 8765

# data/ (SQLite), secrets/ (Telegram session) and persona/ are mounted as volumes
ENTRYPOINT ["python", "-m", "ghostwriter"]
CMD ["run"]
