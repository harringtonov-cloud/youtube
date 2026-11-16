# YouTube Engagement Automation Tester

Инструмент для **технического тестирования автоматизации сайтов**: Playwright на сервере (VPS)
прогоняет сценарии «просмотр / лайк / комментарий» против **встроенной локальной песочницы** —
мини-копии страницы видео со своим бэкендом. Управление и логи — через веб-панель.

## Безопасность: накрутки нет и быть не может

- Браузер открывает **только** `http://127.0.0.1:<порт>/sandbox/...`. Ссылка YouTube нужна лишь для
  извлечения 11-символьного ID, который становится меткой тестового видео.
- Все запросы браузера к другим хостам (включая youtube.com) **отменяются** на уровне Playwright
  и фиксируются в списке заблокированных. Сценарий `safety_guard` проверяет это при каждом запуске.
- Песочница закрыта секретным cookie, которое получает только браузер Playwright.
- Панель защищена `ADMIN_TOKEN`; есть ограничение попыток входа.
- Chromium всегда запускается `headless` на сервере; локальный Chrome не используется.

## Возможности

| Требование | Где реализовано |
|---|---|
| Современный веб-интерфейс (адаптивный, удобен с телефона) | `app/templates/index.html`, `app/static/` |
| Ввод URL YouTube-видео с проверкой | `app/urlutils.py`, `/api/parse` |
| Автоматизация через Playwright на сервере | `app/browser.py`, `app/scenarios.py` |
| Панель сценариев (выбор, запуск, отмена) | вкладка «Новый тестовый прогон» |
| Проверка просмотров / лайков / комментариев | `app/sandbox/`, сценарии `views`, `likes`, `comments` |
| Результаты в реальном времени | SSE `/api/events` |
| Логи успехов и ошибок | консоль в панели + таблица `logs` |
| История тестирования | SQLite `data/history.sqlite3` + скриншоты |
| Исключения и автовосстановление | `app/runner.py`: перезапуск браузера и повтор (до `MAX_ATTEMPTS`) |

Сценарии: `views` (порог воспроизведения, дедупликация, серверная проверка времени), `likes`
(идемпотентность, сохранение, независимые сессии), `comments` (валидация, XSS-защита, лимит частоты),
`recovery` (сбой бэкенда 503 и падение браузера), `safety_guard` (блокировка внешнего трафика).

## Структура

```
yt-engagement-tester/
├── app/
│   ├── main.py          # панель: REST API, SSE, авторизация
│   ├── runner.py        # очередь прогонов, повторы, автовосстановление
│   ├── browser.py       # headless Chromium + блокировка внешних адресов
│   ├── scenarios.py     # тестовые сценарии
│   ├── db.py            # история (SQLite)
│   ├── events.py        # шина событий для SSE
│   ├── urlutils.py      # разбор YouTube-ссылок
│   ├── config.py        # настройки из .env
│   ├── sandbox/         # локальная «страница видео» и её бэкенд
│   ├── templates/       # index.html
│   └── static/          # app.css, app.js
├── tests/test_units.py  # юнит-тесты (без браузера)
├── deploy/yt-tester.service
├── Dockerfile, docker-compose.yml
├── requirements.txt, .env.example
└── data/                # история и скриншоты (создаётся автоматически)
```

## Установка на VPS (Ubuntu/Debian, Python 3.12+)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/<ваш-аккаунт>/yt-engagement-tester.git
cd yt-engagement-tester

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium   # браузер ставится на сервер

cp .env.example .env
nano .env                                 # задайте длинный ADMIN_TOKEN

python -m app
```

Откройте `http://IP_СЕРВЕРА:8000`, введите `ADMIN_TOKEN`. Если порт закрыт — откройте его:
`sudo ufw allow 8000/tcp`.

Если `ADMIN_TOKEN` пуст, токен генерируется при старте и печатается в логе.

### Постоянная работа (systemd)

```bash
sudo useradd -r -m ytester
sudo cp -r . /opt/yt-engagement-tester && sudo chown -R ytester: /opt/yt-engagement-tester
cd /opt/yt-engagement-tester
sudo -u ytester python3 -m venv .venv
sudo -u ytester .venv/bin/pip install -r requirements.txt
sudo -u ytester env PLAYWRIGHT_BROWSERS_PATH=/opt/yt-engagement-tester/.browsers .venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium
sudo cp deploy/yt-tester.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now yt-tester
journalctl -u yt-tester -f
```

### Через Docker

```bash
cp .env.example .env && nano .env
docker compose up -d --build
```

### HTTPS (бесплатно, рекомендуется)

Панель передаёт токен, поэтому лучше открывать её по HTTPS. С доменом, направленным на VPS:

```bash
sudo apt install -y caddy
echo 'tester.example.com { reverse_proxy 127.0.0.1:8000 }' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

После этого поставьте `COOKIE_SECURE=true` в `.env` и перезапустите сервис.

## Использование

1. Войдите по токену.
2. Вставьте ссылку на видео (`watch?v=`, `youtu.be/`, `shorts/`, `embed/`).
3. Отметьте сценарии и нажмите «Запустить тест».
4. Следите за статусами сценариев, счётчиками песочницы и логами в реальном времени.
5. Завершённые прогоны остаются в истории: логи, результаты, скриншоты.

## Настройки (`.env`)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ADMIN_TOKEN` | генерируется | пароль панели |
| `PORT` / `HOST` | 8000 / 0.0.0.0 | адрес панели |
| `MAX_ATTEMPTS` | 3 | попыток на сценарий при сбоях инфраструктуры |
| `SCENARIO_TIMEOUT_S` | 90 | лимит времени сценария |
| `ACTION_TIMEOUT_MS` | 10000 | таймаут действий браузера |
| `SANDBOX_VIEW_THRESHOLD_MS` | 2000 | порог засчитывания просмотра в песочнице |
| `MAX_CONCURRENT_RUNS` | 1 | параллельные прогоны (на малом VPS оставьте 1) |
| `COOKIE_SECURE` | false | true при работе по HTTPS |

## Как работает автовосстановление

- **Сбой инфраструктуры** (таймаут, падение Chromium, закрытая страница): раннер логирует
  причину, перезапускает браузер, ждёт с нарастающей паузой и повторяет сценарий до `MAX_ATTEMPTS`.
- **Провал проверки** (например, счётчик не совпал) — это результат теста, он не повторяется.
- **Перезапуск сервера** во время прогона: незавершённые прогоны помечаются как прерванные.

## Тесты

```bash
python -m unittest discover -s tests -t .
```

Полный сквозной прогон с браузером — запустите панель и выполните все сценарии из интерфейса.

## API

`POST /api/login`, `GET /api/scenarios`, `POST /api/runs`, `GET /api/runs`,
`GET /api/runs/{id}`, `GET /api/runs/{id}/logs`, `POST /api/runs/{id}/cancel`,
`DELETE /api/runs/{id}`, `GET /api/runs/{id}/screenshots`, `GET /api/events` (SSE),
`GET /api/health`. Все, кроме `login` и `health`, требуют авторизации.
