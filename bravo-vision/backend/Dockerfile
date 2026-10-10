FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY bravo ./bravo
RUN pip install --no-cache-dir . && useradd -r -u 10001 jarvis
COPY alembic.ini ./
COPY alembic ./alembic
USER jarvis
EXPOSE 8000
# One worker: rate limits are in-process. Scale out only after moving limiters to Redis.
CMD ["sh", "-c", "alembic upgrade head && uvicorn bravo.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers"]
