# YouTube Engagement Automation Tester v2

Панель управления для работы с **настоящим YouTube через официальный YouTube Data API v3** и
Google OAuth 2.0, плюс отдельная вкладка с автотестами в локальной песочнице (Playwright).
Всё работает на вашем VPS, управление — из браузера (удобно с телефона).

> Данные только настоящие. Если API чего-то не отдаёт (скрытые лайки, отключённые комментарии,
> дизлайки), в интерфейсе показан прочерк и причина — значения не выдумываются.

## Что умеет вкладка «YouTube API»

| Функция | Метод API | Квота | Права |
|---|---|---|---|
| Просмотры, лайки, число комментариев, название, длительность, приватность | `videos.list` | 1 | API-ключ или вход |
| Список комментариев (популярные/новые, постранично) | `commentThreads.list` | 1 | API-ключ или вход |
| Узнать, поставили ли вы лайк | `videos.getRating` | 1 | вход |
| Поставить / снять свой лайк | `videos.rate` | 50 | вход (`youtube`) |
| Написать комментарий, ответить, удалить свой | `commentThreads.insert`, `comments.insert`, `comments.delete` | 50 | **веб-вход** (`youtube.force-ssl`) |
| Изменить название и описание **своего** видео | `videos.update` | 50 | вход |
| Отслеживание статистики во времени (график) | `videos.list` по расписанию | 1 на 50 видео | API-ключ или вход |
| Журнал всех обращений к API и оценка расхода квоты | — | — | — |

### Чего API не умеет — и приложение этого не изображает

- **Накрутка просмотров невозможна**: в API нет операции «добавить просмотр». Просмотры тут можно только **читать**.
- **Дизлайки**: с декабря 2021 YouTube не отдаёт `dislikeCount`.
- **Лайки/комментарии скрыты автором** → поле отсутствует в ответе, в панели «—».
- **Комментарии при входе по коду (device flow)**: Google разрешает в нём только права `youtube` и
  `youtube.readonly`, а комментариям нужен `youtube.force-ssl`. Поэтому комментировать можно только
  после веб-входа (нужен HTTPS-адрес, см. ниже). Панель сама показывает, что доступно текущему входу.
- **Остаток квоты** API не сообщает. Индикатор — оценка по журналу этого приложения; точное значение
  в Google Cloud Console.

### Защита от злоупотреблений (намеренные ограничения)

Операции записи выполняются от имени вашего аккаунта и видны всем, поэтому:
- только по явному нажатию с подтверждением — никаких циклов, расписаний и массовых действий;
- суточный лимит записи (`YT_WRITE_LIMIT_PER_DAY`, по умолчанию 20) и пауза против двойного нажатия;
- лайк не отправляется повторно, если он уже стоит (экономит 50 единиц квоты);
- править можно только видео своего канала; один аккаунт, без ротации;
- только лайк/снятие лайка (дизлайк не реализован).

Используйте на своих видео и в рамках [правил YouTube](https://www.youtube.com/t/terms) и
[условий YouTube API Services](https://developers.google.com/youtube/terms/api-services-terms-of-service).

## Настройка Google (бесплатно)

1. Откройте [Google Cloud Console](https://console.cloud.google.com/) → создайте проект.
2. **APIs & Services → Library** → включите **YouTube Data API v3**.
3. **Credentials → Create credentials → API key** → ограничьте ключ только YouTube Data API v3 →
   значение в `.env` как `YOUTUBE_API_KEY` (даёт чтение без входа).
4. **OAuth consent screen**: тип External, статус Testing, добавьте свой Google-аккаунт в **Test users**.
   Область `youtube` — «чувствительная», поэтому Google покажет предупреждение «приложение не проверено»;
   для личного использования это нормально («Дополнительно → Перейти»).
   *В статусе Testing доступ (refresh token) истекает через 7 дней — тогда нажмите «Войти» снова.*
5. **Credentials → Create credentials → OAuth client ID**:
   - тип **TV and Limited Input devices** → вход по коду, не требует домена (подходит для сервера по IP);
   - тип **Web application** с Authorized redirect URI `https://ВАШ-ДОМЕН/oauth/callback` → нужен для комментариев.

   `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` берите от клиента того типа, которым входите. Можно
   создать оба типа и по очереди менять значения в `.env`.
6. Квота по умолчанию — 10 000 единиц в сутки (обнуляется в полночь по тихоокеанскому времени).
   Чтение стоит 1, лайк/комментарий/правка — 50 единиц.

## Установка на VPS (Ubuntu/Debian, Python 3.12+)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git unzip
unzip yt-engagement-tester.zip && cd yt-engagement-tester     # или git clone вашего репозитория

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # нужен только для вкладки «Тесты в песочнице»

cp .env.example .env && nano .env           # ADMIN_TOKEN, YOUTUBE_API_KEY, GOOGLE_CLIENT_ID/SECRET
python -m app
```

Откройте `http://IP_СЕРВЕРА:8000` (при необходимости `sudo ufw allow 8000/tcp`), войдите по
`ADMIN_TOKEN`. Приложению нужны **исходящие HTTPS-соединения** к `googleapis.com`, `oauth2.googleapis.com`.

### Вход через Google

- **По коду (без домена):** кнопка «Войти по коду» → панель покажет код → на телефоне откройте
  google.com/device, введите код, подтвердите доступ. Панель сама увидит подтверждение.
- **Веб-вход (для комментариев):** нужен HTTPS. Бесплатно: домен на DuckDNS → Caddy сам получит сертификат:
  ```bash
  sudo apt install -y caddy
  echo 'ваш-домен.duckdns.org { reverse_proxy 127.0.0.1:8000 }' | sudo tee /etc/caddy/Caddyfile
  sudo systemctl reload caddy
  ```
  В `.env`: `GOOGLE_REDIRECT_URI=https://ваш-домен.duckdns.org/oauth/callback` и `COOKIE_SECURE=true`.

### Постоянная работа

`deploy/yt-tester.service` (systemd) и `docker-compose.yml` — как в v1: см. комментарии в файлах.
Для systemd: пути в unit-файле `/opt/yt-engagement-tester`, пользователь `ytester`.

## Безопасность

- Панель закрыта `ADMIN_TOKEN` (ограничение попыток входа). Задайте длинное случайное значение.
- OAuth-токены хранятся в `data/history.sqlite3` (права `0600`) — не публикуйте папку `data/`.
  Кнопка «Выйти и отозвать доступ» отзывает токен у Google.
- Веб-вход защищён одноразовым `state`; секрет клиента не уходит в браузер.
- В репозиторий не попадают `.env` и `data/` (см. `.gitignore`).

## Вкладка «Тесты в песочнице» (Playwright)

Автотесты на сервере в headless-Chromium против **локальной** мини-страницы видео со своим бэкендом:
просмотры (порог, дедупликация), лайки, комментарии (валидация, XSS, лимит), автовосстановление
после сбоев, проверка блокировки внешнего трафика. К настоящему YouTube эта вкладка не обращается:
браузер заперт на loopback, а внешние запросы отменяются. Подробности — `app/sandbox/`, `app/scenarios.py`.

## Структура

```
app/
├── main.py            # панель: REST, SSE, вход по токену
├── youtube/
│   ├── http.py        # HTTP-клиент (stdlib), разбор ошибок Google, повторы только для GET
│   ├── oauth.py       # OAuth 2.0: device flow + веб-вход, обновление и отзыв токенов
│   ├── service.py     # YouTube Data API: чтение, лайк, комментарии, правка, квота, поллер
│   └── routes.py      # HTTP-маршруты вкладки
├── runner.py, browser.py, scenarios.py, sandbox/   # песочница и Playwright
├── db.py              # SQLite: история тестов, журнал API, отслеживание, токены
├── templates/, static/  # интерфейс (index.html, app.js, yt.js, app.css)
tests/                 # юнит-тесты и тестовый двойник Google
deploy/, Dockerfile, docker-compose.yml, requirements.txt, .env.example
```

## Тесты

```bash
python -m unittest discover -s tests -t .
```

Тесты YouTube-слоя работают против **тестового двойника Google** (`tests/mock_google.py`): он
повторяет документированное поведение API (коды ошибок, требуемые права, стирание пропущенных
полей при `videos.update`). Это проверка протокола и логики; **с вашим реальным Google-проектом
приложение нужно проверить один раз самому** (вход, чтение видео, лайк на своём аккаунте).

## Частые проблемы

| Симптом | Причина / решение |
|---|---|
| `quotaExceeded` | квота проекта исчерпана до полуночи PT; снизьте частоту опроса или запросите повышение |
| «нет прав» у комментариев | вход по коду не даёт `youtube.force-ssl` → войдите через веб-вход |
| `invalid_grant` / просит войти снова | доступ отозван или истёк 7-дневный срок режима Testing |
| `invalid_client` | ID/секрет не от клиента того типа, которым входите |
| `commentsDisabled` | владелец отключил комментарии — API их не отдаёт |
| лайки «—» | автор скрыл счётчик лайков |
| `accessNotConfigured` / 403 при запросе | не включён YouTube Data API v3 в проекте |

## API панели

`GET /api/yt/status`, `GET /api/yt/video?url=`, `GET /api/yt/comments?video=`,
`POST /api/yt/rate`, `POST /api/yt/comment`, `POST /api/yt/reply`, `DELETE /api/yt/comment/{id}`,
`POST /api/yt/video/update`, `GET|POST /api/yt/watch`, `POST /api/yt/watch/refresh`,
`GET /api/yt/actions`, `POST /api/yt/auth/device/start|cancel`, `GET /api/yt/auth/web/start`,
`POST /api/yt/auth/logout`, `GET /oauth/callback`, а также маршруты песочницы `/api/runs…`, `/api/events` (SSE).
Все, кроме `/api/login`, `/api/health`, `/oauth/callback`, требуют авторизации.
