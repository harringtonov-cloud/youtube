"""Запуск: python -m app"""

import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        log_level=s.log_level,
        # SSE-соединения живут долго, прокси/клиенты их держат сами
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
