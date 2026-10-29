FROM python:3.13-alpine

WORKDIR /app

RUN pip install --no-cache-dir "aiohttp>=3.12.14,<4"

COPY app.py /app/app.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/app.py"]
