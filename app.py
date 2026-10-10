"""토큰 트래커 — Claude Code / Codex / Gemini usage + remaining limits.

    python app.py              위젯 + 대시보드 (기본)
    python app.py --dashboard  위젯 없이 대시보드만 (브라우저로 열림)
    python app.py --scan       로그를 한 번 읽고 결과만 출력
    python app.py --doctor     어떤 로그를 찾았는지, 한도 조회가 되는지 점검
    python app.py --export DIR 대시보드 아티팩트용 JSON 파일 내보내기
"""
import argparse
import json
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import paths, stats  # noqa: E402
from tracker.collectors import scan_all  # noqa: E402
from tracker.db import DB  # noqa: E402
from tracker.engine import Engine  # noqa: E402
from tracker.limits import all_limits  # noqa: E402
from tracker.server import serve  # noqa: E402


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/limits", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def cmd_scan():
    db = DB()
    t = time.time()
    rep = scan_all(db)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"\n{time.time() - t:.1f}초 · DB: {db.path}")
    tod = stats.today(db)
    print("오늘:", {k: (v["tokens"], round(v["cost"], 2)) for k, v in tod.items()})


def cmd_doctor():
    db = DB()
    print("데이터 폴더 :", paths.data_dir())
    print("Claude 로그 :", [str(p) for p in paths.claude_project_dirs()] or "못 찾음")
    print("Claude 인증 :", paths.claude_credentials_file() or "못 찾음")
    print("Codex 로그  :", [str(p) for p in paths.codex_session_dirs()] or "못 찾음")
    print("Gemini 폴더 :", paths.gemini_root(), "(있음)" if paths.gemini_root().exists() else "(없음)")
    print("\n로그 읽는 중…")
    print(json.dumps(scan_all(db), ensure_ascii=False, indent=2))
    print("\n수집된 기록:")
    for s, c in stats.coverage(db).items():
        print(f"  {s:7s} {c['n']:>8,}건  {fmt_ts(c['first'])} ~ {fmt_ts(c['last'])}")
    print("\n한도:")
    for s, l in all_limits(db, force=True).items():
        wins = ", ".join(f"{w['label']} {w['used_percent']:.0f}% 사용" for w in l.get("windows", []))
        print(f"  {s:7s} [{l.get('status')}] {wins or '-'} {l.get('message', '')}")


def main():
    ap = argparse.ArgumentParser(description="토큰 트래커")
    ap.add_argument("--dashboard", action="store_true", help="위젯 없이 대시보드만")
    ap.add_argument("--no-browser", action="store_true", help="대시보드를 자동으로 열지 않음")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--export", metavar="FOLDER", help="대시보드용 JSON 내보내기")
    ap.add_argument("--port", type=int)
    ap.add_argument("--install", action="store_true", help="바탕화면·시작 메뉴에 아이콘 만들기 (Windows)")
    ap.add_argument("--startup", action="store_true", help="--install과 함께: 로그인할 때 자동 실행")
    ap.add_argument("--uninstall", action="store_true", help="만든 아이콘 지우기")
    ap.add_argument("--wait-port", action="store_true", help=argparse.SUPPRESS)  # after self-update
    a = ap.parse_args()

    if a.install:
        from tracker.shortcuts import install
        return install(startup=a.startup)
    if a.uninstall:
        from tracker.shortcuts import uninstall
        return uninstall()

    if a.scan:
        return cmd_scan()
    if a.doctor:
        return cmd_doctor()
    if a.export:
        from tracker.export import export
        db = DB()
        scan_all(db)
        print(export(db, a.export), "->", a.export)
        return

    port = a.port or paths.load_config().get("port", 8765)
    if a.wait_port:  # the old copy is still closing after an update
        for _ in range(30):
            if not already_running(port):
                break
            time.sleep(0.5)
    if already_running(port):  # second launch: just open the dashboard
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return

    engine = Engine()
    engine.refresh()
    engine.start()

    use_widget = not a.dashboard
    if use_widget:
        try:
            from tracker.widget import Widget
        except ImportError:
            print("tkinter가 없어 대시보드 모드로 실행합니다")
            use_widget = False

    if use_widget:
        _, url = serve(engine, port=port, background=True)
        Widget(engine, url).run()
    else:
        httpd, url = serve(engine, port=port, background=True)
        print(f"대시보드: {url}   (끄려면 Ctrl+C)")
        if not a.no_browser:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            engine.stop()


if __name__ == "__main__":
    main()
