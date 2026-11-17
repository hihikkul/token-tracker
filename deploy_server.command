#!/bin/bash
# 쌀숭이 랭킹 서버를 내 Cloudflare 계정에 올립니다 (처음 한 번)
cd "$(dirname "$0")" || exit 1
PYTHON=''
for candidate in python3 python "$HOME/anaconda3/bin/python" "$HOME/miniconda3/bin/python" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo 'Python 3.10 이상을 실행할 수 없어요. https://www.python.org/downloads/ 에서 설치하세요.'
elif ! node --version >/dev/null 2>&1 || ! npx --version >/dev/null 2>&1; then
    echo 'Node.js와 npx를 실행할 수 없어요. https://nodejs.org 에서 LTS 버전을 설치하세요.'
else
    "$PYTHON" server/deploy.py
fi
read -n 1 -s -r -p "아무 키나 누르면 닫혀요"
