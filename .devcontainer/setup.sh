#!/usr/bin/env bash
# Выполняется один раз при первом создании Codespace (onCreateCommand).
# Создаёт виртуальное окружение, ставит зависимости проекта и браузер Playwright.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== Создаю виртуальное окружение (.venv) =="
python3 -m venv .venv
source .venv/bin/activate

echo "== Устанавливаю зависимости проекта (requirements.txt) =="
pip install --upgrade pip --quiet
pip install -r requirements.txt

echo "== Устанавливаю браузер Chromium для Playwright (с системными зависимостями) =="
# Сначала системные библиотеки (нужен sudo — в devcontainer он есть без пароля),
# затем сам браузер от имени текущего пользователя.
sudo "$(pwd)/.venv/bin/playwright" install-deps chromium
.venv/bin/playwright install chromium

echo "== Готовлю .env, если его ещё нет =="
if [ ! -f .env ]; then
  cp .env.example .env
  # Генерируем случайный пароль панели, чтобы не оставлять поле пустым.
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")
  sed -i "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=${TOKEN}/" .env
  echo "Создан .env. Пароль панели (ADMIN_TOKEN): ${TOKEN}"
  echo "Он также сохранён в файле .env — посмотреть его можно командой: cat .env"
fi

mkdir -p data
echo "== Настройка окружения завершена =="
