#!/usr/bin/env python3
"""
Автогенерация .env с криптостойкими секретами.

Запуск:
    python3 scripts/setup_env.py

Что делает:
1. Читает .env.example
2. Заменяет CHANGE_ME_xxx плейсхолдеры на случайные секреты
3. Сохраняет .env (если не существует)

Если .env УЖЕ существует — скрипт спросит подтверждение.
"""

import os
import secrets
import sys
from pathlib import Path


def main():
    base = Path(__file__).parent.parent
    example = base / ".env.example"
    target = base / ".env"

    if not example.exists():
        print(f"ERROR: .env.example not found at {example}")
        sys.exit(1)

    if target.exists():
        print(f".env already exists at {target}")
        answer = input("Overwrite? Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Cancelled.")
            sys.exit(0)

    text = example.read_text()

    replacements = {
        "CHANGE_ME_to_a_long_random_string_at_least_32_chars": secrets.token_hex(32),
        "CHANGE_ME_strong_password": secrets.token_hex(16),
        "CHANGE_ME_strong_redis_password": secrets.token_hex(16),
    }

    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)

    target.write_text(text)
    os.chmod(target, 0o600)

    print("OK — .env created with random secrets")
    print(f"  WEBHOOK_SECRET:     {len(replacements['CHANGE_ME_to_a_long_random_string_at_least_32_chars'])} chars")
    print(f"  POSTGRES_PASSWORD:  {len(replacements['CHANGE_ME_strong_password'])} chars")
    print(f"  REDIS_PASSWORD:     {len(replacements['CHANGE_ME_strong_redis_password'])} chars")


if __name__ == "__main__":
    main()
