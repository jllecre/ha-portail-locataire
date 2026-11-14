FROM python:3.13-alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir "aiohttp>=3.12.14,<4"

COPY app.py /app/app.py
COPY floor-plan.png /app/floor-plan.png

CMD ["python", "/app/app.py"]
