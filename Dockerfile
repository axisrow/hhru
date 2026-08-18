FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

ENV PYTHONPATH=/app/src

ENTRYPOINT ["python3", "-m", "hhru_bot.cli"]
CMD ["--headless", "run"]
