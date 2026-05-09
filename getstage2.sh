#!/bin/bash
# Stage 2 deploy script — скачивает 5 файлов с GitHub в правильные места
# Запуск: bash /root/getstage2.sh

set -e

cd /root/maskara-bot

BASE="https://raw.githubusercontent.com/ViktoriaMaas1/maskara-bot/main"

echo ">>> Downloading Stage 2 files..."

curl -fsSL -o "app/bybit/rest_client.py" "$BASE/app/bybit/rest_client.py"
echo "  rest_client.py: $(wc -c < app/bybit/rest_client.py) bytes"

curl -fsSL -o "app/bybit/exceptions.py" "$BASE/app/bybit/exceptions.py"
echo "  exceptions.py: $(wc -c < app/bybit/exceptions.py) bytes"

curl -fsSL -o "app/api/health.py" "$BASE/app/api/health.py"
echo "  health.py: $(wc -c < app/api/health.py) bytes"

curl -fsSL -o "scripts/test_bybit.py" "$BASE/scripts/test_bybit.py"
echo "  test_bybit.py: $(wc -c < scripts/test_bybit.py) bytes"

curl -fsSL -o "tests/test_bybit_client.py" "$BASE/tests/test_bybit_client.py"
echo "  test_bybit_client.py: $(wc -c < tests/test_bybit_client.py) bytes"

echo ""
echo ">>> All files downloaded successfully!"
