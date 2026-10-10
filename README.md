<img src="assets/token-tracker.png" width="72" align="left">

# token-tracker

<br clear="left">

Claude Code, Codex, Gemini CLI의 토큰 사용량과 남은 한도를 내 PC에서 바로 보는 도구예요.
화면 구석에 떠 있는 **한도 위젯**과 브라우저로 여는 **사용량 대시보드** 두 가지로 되어 있어요.

- 설치할 패키지 없음 (Python 표준 라이브러리만 사용)
- 모든 기록은 내 PC 안에만 저장돼요. 대화 내용은 읽지 않고 토큰 수만 셉니다.
- Windows와 macOS에서 돌아가요. Mac은 [Releases](https://github.com/hihikkul/token-tracker/releases/latest)의 DMG로 설치해요.

## 설치

1. **Python 3.10 이상**이 필요해요. 없으면 [python.org](https://www.python.org/downloads/)에서 설치하고, 설치 화면에서 *Add python.exe to PATH*를 체크하세요. (Anaconda도 됩니다)
2. 이 페이지 위쪽 초록색 **Code → Download ZIP**으로 받아서 원하는 폴더에 압축을 풀어요.
   git을 쓴다면 `git clone https://github.com/hihikkul/token-tracker.git`
3. 폴더 안의 **`install.bat`** 을 한 번 더블클릭해요. 바탕화면과 시작 메뉴에 **token-tracker** 아이콘이 생겨요.
4. 이제부터는 바탕화면의 **token-tracker** 아이콘을 누르면 실행돼요.
   - 화면 구석에 한도 위젯이 뜨고, 이미 켜져 있을 때 누르면 대시보드가 열려요.
   - 처음 켤 때는 기존 로그를 한 번 쭉 읽느라 몇 초에서 1분쯤 걸려요.
5. (선택) PC를 켤 때마다 자동으로 띄우고 싶으면 `install.bat` 대신 **`install_startup.bat`** 을 실행해요. 아이콘을 전부 지우려면 `uninstall.bat`.

압축을 푼 폴더는 지우거나 옮기지 마세요. 아이콘이 그 폴더를 가리켜요. 옮겼다면 `install.bat`을 다시 실행하면 돼요.

**업데이트는 자동이에요.** 위젯이 켜져 있으면 시작 직후와 1시간마다 GitHub에 새 버전이 있는지 보고, 있으면 받아서 스스로 다시 켜져요. 따로 누를 건 없어요. 모은 기록과 설정은 `%LOCALAPPDATA%\TokenTracker`에 따로 있어서 그대로 남아요. 끄려면 설정에 `"auto_update": false` (git clone으로 받았다면 자동 업데이트 대신 `git pull`).

## macOS

1. [Releases](https://github.com/hihikkul/token-tracker/releases/latest)에서 내 Mac에 맞는 DMG를 받아요 (M1 이후 = `arm64`, Intel = `x86_64`).
2. 열어서 **Token Tracker**를 Applications로 끌어다 놓고 실행해요.
3. 서명하지 않은 앱이라 처음엔 "확인되지 않은 개발자" 경고가 떠요. **시스템 설정 → 개인정보 보호 및 보안 → 그래도 열기**를 누르면 돼요.
4. Claude Code 로그인 정보는 키체인에 있어서, 처음에 키체인 접근을 물어보면 **허용**을 눌러 주세요 (한도 조회용, 읽기만 해요).

Mac 앱은 스스로 덮어쓸 수 없어서, 새 버전이 나오면 위젯 우클릭 메뉴에 *Download vX* 가 생겨요. 기록과 설정은 `~/Library/Application Support/TokenTracker`에 있어요.

## 위젯 (Usage Limits)

- 화면 구석에 작업 표시줄 높이의 납작한 막대 하나로 떠요: `◔ Claude 72%  │  ◕ Codex 88%` (각 도구의 **5시간 한도**에서 남은 양)
- 게이지 색: 넉넉할 땐 도구 색(Claude 주황, Codex 파랑), 절반 넘게 쓰면 노랑 → 주황(75%) → 빨강(90%)
- 마우스를 올리면 5h / Weekly 등 모든 한도와 리셋까지 남은 시간이 보여요
- 드래그로 이동 · 더블클릭으로 대시보드 · 우클릭 메뉴 (Open Dashboard, Refresh Now, Minimize to Taskbar, Always on Top, Quit)
- **작업 표시줄에도 떠요.** 아이콘이 가장 빠듯한 5h 남은 %를 게이지와 숫자로 보여 주고, 마우스를 올리면 `Claude 72% · Codex 88% (5h)`가 보여요. 끄려면 설정에 `"widget": {"taskbar": false}`.
- 글꼴은 Windows 기본 Segoe UI Variable(Windows 10은 Segoe UI)이라 따로 설치할 게 없어요.
- Claude 한도는 5분마다 Anthropic에 직접 조회하고, Codex 한도는 Codex가 마지막 응답 때 로그에 남긴 값이에요 (Codex를 쓰지 않는 동안은 갱신되지 않음).

## 대시보드

위젯의 ▤ 버튼, 또는 `python app.py --dashboard`. 주소는 http://127.0.0.1:8765/ 이고 내 PC에서만 열려요.

- 맨 위에 위젯과 같은 남은 한도 카드
- 총 사용 토큰, 최근 30일 토큰, 최근 30일 API 환산 비용
- 요일과 시간대별 사용량, 일별 모델별 토큰
- 입력·출력·캐시 비율, 캐시 적중률, 캐시로 아낀 비용
- 누적 비용(직전 같은 기간과 비교), 월별, 프로젝트, 모델
- 기간(7/30/90일/전체), 도구(Claude/Codex), 단위(토큰/$) 전환. 모델이나 프로젝트를 누르면 그것만 걸러 봐요.

## 쌀먹 비율

대시보드 위쪽 카드. 같은 토큰을 API 정가로 썼다면 냈을 금액 ÷ 그 기간만큼의 구독료예요 (오늘 / 이번 주 / 이번 달).

- 구독료는 한도 조회에서 읽은 요금제의 정가로 정해요: Claude Pro $20 · Max 5x $100 · Max 20x $200, ChatGPT(Codex) Plus $20 · Pro $200.
- Gemini는 요금제를 읽을 방법이 없어서, 카드의 *구독료 수정*에서 넣어야 계산에 들어가요. 다른 값도 거기서 덮어쓸 수 있어요.
- 기간 구독료 = 월 구독료 × 12 / 365 × 지난 날수.

## 쌀숭이 랭킹

대시보드 오른쪽 위 **랭킹** 버튼. 친구들과 그룹을 만들어 일일 / 주간 / 월간 쌀먹 비율을 겨뤄요. 그룹 1등이 되면 처음 들어갈 때 축하해 줘요.

- *새 그룹 만들기*로 초대 코드를 받고, 친구는 *초대 코드로 참가*.
- 서버로 가는 건 닉네임, 요금제 이름, 기간별 API 환산 금액과 구독료 몫뿐이에요. 대화 내용, 프로젝트, 모델별 기록은 보내지 않아요. 15분마다 갱신돼요.
- 서버는 Cloudflare Workers + D1 (`server/`). 처음 한 번 `deploy_server.bat` (Mac은 `deploy_server.command`)로 내 Cloudflare 계정에 올려요. Node.js가 필요하고, 브라우저에서 Cloudflare 로그인을 한 번 허용하면 끝이에요.

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
- `widget.font`, `widget.number_font`: 글꼴 (비워 두면 Segoe UI Variable)
- `widget.taskbar`: 작업 표시줄 버튼과 % 아이콘 (기본 `true`)
- `extra_paths`: 기본 위치 밖의 로그 폴더 추가. 예: `"claude": ["D:/backup/.claude/projects"]`
- `port` (8765), `scan_interval_sec` (60), `limits_interval_sec` (300)
- 환경변수 `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_HOME`, `TOKENTRACKER_HOME`도 따라가요.

## 문제가 생기면

`doctor.bat`을 실행하면 어떤 로그를 찾았는지, 몇 건 읽었는지, 한도 조회가 되는지 보여줘요.

- Claude 한도 엔드포인트는 비공식이라 바뀌면 한도 표시가 멈출 수 있어요 (사용량 집계는 계속 됨).
- Claude 로그인 토큰이 만료되면 트래커가 Claude Code와 같은 방식으로 갱신해서 같은 파일에 다시 저장해요. 갱신도 실패하면 Claude Code에서 `/login`을 한 번 해 주세요. 이 동작을 끄려면 설정에 `"refresh_claude_token": false`.

## 라이선스

MIT. 포함된 D3는 ISC 라이선스입니다.
