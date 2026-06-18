# Nozimaxon Telegram bot — production image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Bog'liqliklarni alohida layer'da o'rnatamiz (cache uchun)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ilova kodi va statik resurslar
COPY src/ ./src/
COPY pos/ ./pos/
COPY assets/ ./assets/
COPY scripts/ ./scripts/

# Ma'lumotlar katalogi (volume sifatida montaj qilinadi) va root bo'lmagan user
RUN mkdir -p data/media \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

# Konteyner ichida bot shu portni tinglaydi (webhook_port)
EXPOSE 8080

# Webhook rejimida ishga tushadi — Telegram va AmoCRM webhook'larini qabul qiladi
CMD ["python", "-m", "src.main", "--webhook"]
