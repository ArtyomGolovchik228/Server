# --- Dockerfile для FastAPI сервера English VR Authorization ---
FROM python:3.11-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Системные пакеты для bcrypt и curl
RUN apk add --no-cache gcc musl-dev libffi-dev curl

# Сначала зависимости (для кэша слоёв Docker)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Затем приложение
COPY . .

# Каталоги, которые сервер ожидает увидеть на старте
RUN mkdir -p /app/logs /app/data /app/static

EXPOSE 8000

# Healthcheck — стучимся в /health (есть в main.py)
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# В продакшене запускаем БЕЗ --reload (он только для разработки)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
