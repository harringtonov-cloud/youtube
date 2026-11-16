FROM python:3.12-slim

WORKDIR /srv/app
COPY requirements.txt .
# Браузер ставится той же версии Playwright, что и библиотека
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY app ./app
RUN mkdir -p data

ENV HOST=0.0.0.0 PORT=8000 DATA_DIR=/srv/app/data
EXPOSE 8000
CMD ["python", "-m", "app"]
