#!/usr/bin/env bash
# Выполняется при КАЖДОМ запуске Codespace: после возобновления из паузы,
# после перезапуска машины, после ребилда контейнера без полного onCreate.
# Задача — восстановить рабочее окружение, если оно почему-то отсутствует
# (например, после ребилда контейнера .venv пересоздаётся с нуля).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -x .venv/bin/python ]; then
  echo "== .venv не найден — выполняю полную настройку заново =="
  bash .devcontainer/setup.sh
  exit 0
fi

source .venv/bin/activate

# Если requirements.txt менялся с прошлого запуска — подтягиваем изменения (быстро, если всё уже стоит).
pip install -r requirements.txt --quiet

# Playwright мог быть добавлен/обновлён или браузер мог не сохраниться между запусками — проверяем тихо.
if ! .venv/bin/playwright install chromium --dry-run >/dev/null 2>&1; then
  sudo .venv/bin/playwright install-deps chromium || true
fi
.venv/bin/playwright install chromium

[ -f .env ] || cp .env.example .env
mkdir -p data

echo "== Окружение готово. Запустить панель: =="
echo "   source .venv/bin/activate && python -m app"
