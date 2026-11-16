"""Запуск: python -m app"""

import os

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
        # За прокси хостинга (Render) настоящий IP клиента приходит в X-Forwarded-For;
        # иначе защита от подбора токена считала бы всех посетителей одним адресом.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS") or ("*" if os.environ.get("RENDER") else "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
