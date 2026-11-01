FROM python:3.12-slim

WORKDIR /app

# Установка системных зависимостей для сборки некоторых пакетов и работы с PDF/шрифтами
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]