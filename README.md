# Token Tracker

Claude Code, Codex, Gemini CLI의 토큰 사용량과 남은 한도를 내 PC에서 바로 보는 도구예요.
화면 구석에 떠 있는 **한도 위젯**과 브라우저로 여는 **사용량 대시보드** 두 가지로 되어 있어요.

- 설치할 패키지 없음 (Python 표준 라이브러리만 사용)
- 모든 기록은 내 PC 안에만 저장돼요. 대화 내용은 읽지 않고 토큰 수만 셉니다.
- Windows 기준으로 만들었어요.

## 설치

1. **Python 3.10 이상**이 필요해요. 없으면 [python.org](https://www.python.org/downloads/)에서 설치하고, 설치 화면에서 *Add python.exe to PATH*를 체크하세요. (Anaconda도 됩니다)
2. 이 페이지 위쪽 초록색 **Code → Download ZIP**으로 받아서 원하는 폴더에 압축을 풀어요.
   git을 쓴다면 `git clone https://github.com/hihikkul/token-tracker.git`
3. 폴더 안의 **`run.bat`** 을 더블클릭하면 끝.
   - 처음 켤 때는 기존 로그를 한 번 쭉 읽느라 몇 초에서 1분쯤 걸려요.
4. (선택) **`install_startup.bat`** 을 한 번 실행하면 PC를 켤 때마다 자동으로 떠요. 해제는 `uninstall_startup.bat`.

업데이트할 때는 위젯을 끄고(✕) 새로 받은 파일을 같은 폴더에 덮어쓴 뒤 다시 `run.bat`. 모은 기록은 `%LOCALAPPDATA%\TokenTracker`에 따로 있어서 그대로 남아요.

## 위젯 (Usage Limits)

- 도구와 요금제별로 5h / Weekly 한도가 몇 % 남았는지, 언제 리셋되는지 (`10/10 · Resets in 2h 13m`)
- 막대 = 남은 양, 색 = 사용량 (초록 < 50% < 노랑 < 75% < 주황 < 90% < 빨강)
- ▾/▸ 자세히·간단히 · ⟳ 즉시 조회 · ▤ 대시보드 열기 · ✕ 종료 · 제목줄 드래그로 이동 · 우클릭 메뉴
- Claude 한도는 5분마다 Anthropic에 직접 조회하고, Codex 한도는 Codex가 마지막 응답 때 로그에 남긴 값이에요 (Codex를 쓰지 않는 동안은 갱신되지 않음).

## 대시보드

위젯의 ▤ 버튼, 또는 `python app.py --dashboard`. 주소는 http://127.0.0.1:8765/ 이고 내 PC에서만 열려요.

- 총 사용 토큰, 최근 30일 토큰, 최근 30일 API 환산 비용
- 요일과 시간대별 사용량, 일별 모델별 토큰
- 입력·출력·캐시 비율, 캐시 적중률, 캐시로 아낀 비용
- 누적 비용(직전 같은 기간과 비교), 월별, 프로젝트, 모델
- 기간(7/30/90일/전체), 도구(Claude/Codex), 단위(토큰/$) 전환. 모델이나 프로젝트를 누르면 그것만 걸러 봐요.

## 어디서 읽나요

| 도구 | 사용량 | 한도 |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | `api.anthropic.com/api/oauth/usage` (Claude Code 로그인 토큰 사용, 만료되면 갱신) |
| Codex | `~/.codex/sessions`, `archived_sessions` | 같은 로그의 `rate_limits` (네트워크 사용 안 함, 요금제별로 표시) |
| Gemini CLI | `~/.gemini/tmp/*/chats/*.json` | 없음 |

- claude.ai 웹·앱 대화는 PC에 토큰 기록이 남지 않아서 집계되지 않아요.
- Claude Code는 기본 30일이 지나면 로그를 지우지만, 트래커가 한 번 읽은 기록은 `usage.db`에 계속 남아요. 켜 둘수록 기록이 길어집니다.
- 비용은 **API 정가로 환산한 추정치**예요. 구독 실제 청구액이 아닙니다. 단가는 `prices.json`에서 고칠 수 있어요 (일부 모델은 공식이 아닌 가격표 기준이라 확인을 권해요).

## 설정

`%LOCALAPPDATA%\TokenTracker\config.json` (처음 실행할 때 생겨요)

- `widget.show`: 위젯에 보일 도구 (기본 `["claude", "codex"]`)
- `widget.font`, `widget.number_font`: 글꼴 (기본 `Calibri`)
- `extra_paths`: 기본 위치 밖의 로그 폴더 추가. 예: `"claude": ["D:/backup/.claude/projects"]`
- `port` (8765), `scan_interval_sec` (60), `limits_interval_sec` (300)
- 환경변수 `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_HOME`, `TOKENTRACKER_HOME`도 따라가요.

## 문제가 생기면

`doctor.bat`을 실행하면 어떤 로그를 찾았는지, 몇 건 읽었는지, 한도 조회가 되는지 보여줘요.

- Claude 한도 엔드포인트는 비공식이라 바뀌면 한도 표시가 멈출 수 있어요 (사용량 집계는 계속 됨).
- Claude 로그인 토큰이 만료되면 트래커가 Claude Code와 같은 방식으로 갱신해서 같은 파일에 다시 저장해요. 갱신도 실패하면 Claude Code에서 `/login`을 한 번 해 주세요. 이 동작을 끄려면 설정에 `"refresh_claude_token": false`.

## 라이선스

MIT. 포함된 D3는 ISC 라이선스입니다.
