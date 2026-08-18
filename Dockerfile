FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

RUN pip install --no-cache-dir -e .

ENTRYPOINT ["hhru"]
CMD ["--headless", "run"]
