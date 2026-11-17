# Hugging Face Space (Docker SDK). Забирает код из публичного репозитория GitHub при сборке.
# Чтобы обновить приложение после нового коммита: Settings → Factory rebuild.
FROM python:3.12-slim

ARG REPO=https://github.com/harringtonov-cloud/youtube.git
ARG BRANCH=main

# Системные пакеты и браузер ставятся ОТ ROOT, затем контейнер переключается на пользователя 1000
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
WORKDIR /opt/src
RUN git clone --depth 1 --branch "$BRANCH" "$REPO" app-src \
    && pip install --no-cache-dir -r app-src/requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face запускает контейнер от пользователя с UID 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user
WORKDIR /home/user/app
RUN cp -r /opt/src/app-src/. /home/user/app/ && mkdir -p /home/user/app/data

ENV HOST=0.0.0.0 \
    PORT=7860 \
    DATA_DIR=/home/user/app/data \
    COOKIE_SECURE=true \
    FORWARDED_ALLOW_IPS=* \
    PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["python", "-m", "app"]
