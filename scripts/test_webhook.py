#!/usr/bin/env python3
"""
Утилита для отправки тестового сигнала в webhook.

Использование:
    # Дефолт: BUY BTCUSDT 3m на localhost
    python3 scripts/test_webhook.py

    # Свой URL и параметры
    python3 scripts/test_webhook.py --url http://server.com:8000 --side SELL --symbol ETHUSDT

    # Несколько подряд (проверка дедупликации и rate limit)
    python3 scripts/test_webhook.py --count 10

Читает WEBHOOK_SECRET из .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError, URLError


def load_env() -> dict[str, str]:
    """Простой парсер .env (без зависимостей)."""
    env_file = Path(__file__).parent.parent / ".env"
    env: dict[str, str] = {}
    if not env_file.exists():
        print("❌ .env не найден. Скопируй: cp .env.example .env")
        sys.exit(1)
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_signal(url: str, payload: dict, timeout: int = 10) -> tuple[int, dict]:
    """Отправляет POST. Возвращает (status, json_body)."""
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except URLError as e:
        print(f"❌ Сервер недоступен: {e.reason}")
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description="Отправить тестовый webhook сигнал")
    parser.add_argument("--url", default="http://localhost:8000",
                        help="Адрес бота (default: http://localhost:8000)")
    parser.add_argument("--symbol", default="BTCUSDT", choices=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--side", default="BUY", choices=["BUY", "SELL"])
    parser.add_argument("--timeframe", default="3m", choices=["1m", "3m", "15m", "1h"])
    parser.add_argument("--strategy", default="liquidity_sweep")
    parser.add_argument("--count", type=int, default=1,
                        help="Сколько сигналов отправить (для проверки дедупа/rate limit)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Задержка между сигналами в секундах")
    parser.add_argument("--bad-secret", action="store_true",
                        help="Отправить с неправильным secret (для проверки 401)")
    args = parser.parse_args()

    env = load_env()
    secret = "wrong_secret_for_test_at_least_16chars" if args.bad_secret \
             else env.get("WEBHOOK_SECRET", "")

    if not secret:
        print("❌ WEBHOOK_SECRET не задан в .env")
        sys.exit(1)

    payload = {
        "secret": secret,
        "symbol": args.symbol,
        "side": args.side,
        "timeframe": args.timeframe,
        "strategy": args.strategy,
    }

    target = f"{args.url.rstrip('/')}/webhook"
    print(f"→ {target}")
    print(f"  payload: {json.dumps({**payload, 'secret': '***'}, indent=2)}\n")

    for i in range(1, args.count + 1):
        status, body = send_signal(target, payload)
        marker = "✅" if status in (200, 202) else "⚠️ " if status == 429 else "❌"
        print(f"{marker} [{i}/{args.count}] HTTP {status} → "
              f"status={body.get('status')} message={body.get('message', body.get('detail', ''))[:80]}")
        if args.delay > 0 and i < args.count:
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
