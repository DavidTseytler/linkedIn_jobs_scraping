FROM python:3.9-slim

WORKDIR /app

# Копируем только нужные файлы
COPY main.py .
COPY requirements.txt .
COPY templates/ ./templates/

# Создаем пустые статические папки
RUN mkdir -p static/css static/js

RUN pip install --no-cache-dir -r requirements.txt

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
