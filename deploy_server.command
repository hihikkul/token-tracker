#!/bin/bash
# 쌀숭이 랭킹 서버를 내 Cloudflare 계정에 올립니다 (처음 한 번)
cd "$(dirname "$0")" && python3 server/deploy.py
read -n 1 -s -r -p "아무 키나 누르면 닫혀요"
