"""토큰 트래커 — Claude Code / Codex / Gemini usage + remaining limits.

    python app.py              위젯 + 대시보드 (기본)
    python app.py --dashboard  위젯 없이 대시보드만 (브라우저로 열림)
    python app.py --scan       로그를 한 번 읽고 결과만 출력
    python app.py --doctor     어떤 로그를 찾았는지, 한도 조회가 되는지 점검
    python app.py --export DIR 대시보드 아티팩트용 JSON 파일 내보내기
"""
import argparse
import os
import json
import sys
import time
import socket
import urllib.request
import webbrowser
from urllib.parse import urlparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import paths, sources, stats, tls  # noqa: E402
from tracker.collectors import scan_all  # noqa: E402
from tracker.db import DB  # noqa: E402
from tracker.engine import Engine  # noqa: E402
from tracker.limits import all_limits  # noqa: E402
from tracker.server import serve  # noqa: E402


class InstanceLock:
    """Keep the lock file open for the whole app lifetime, independent of its HTTP port."""
    def __init__(self):
        self.folder = paths.data_dir()
        self.file = (self.folder / "instance.lock").open("a+b")
        self.locked = False
        self.file.seek(0, os.SEEK_END)
        if not self.file.tell():
            self.file.write(b"\0")
            self.file.flush()

    def acquire(self):
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.locked = True
        # A previous owner may have crashed; do not send another launch to its stale port.
        (self.folder / "instance.json").unlink(missing_ok=True)
        return True

    def port(self):
        try:
            port = json.loads((self.folder / "instance.json").read_text(encoding="utf-8"))["port"]
            return port if type(port) is int and 0 < port < 65536 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def publish(self, url):
        target = self.folder / "instance.json"
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"port": urlparse(url).port}), encoding="utf-8")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    def close(self):
        if self.locked:
            try:
                (self.folder / "instance.json").unlink(missing_ok=True)
            finally:
                self.file.close()  # closing releases the OS lock, including on process exit
                self.locked = False
        else:
            self.file.close()


def port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def tracker_answers(port):
    """True when a Token Tracker dashboard (any version) answers on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/limits", timeout=0.8) as r:
            return r.status == 200 and r.headers.get_content_type() == "application/json"
    except Exception:
        return False


def claim_instance(port, wait_port=False):
    instance = InstanceLock()
    deadline = time.monotonic() + 15
    try:
        while not instance.acquire():
            actual = instance.port()
            if not wait_port and actual is not None:
                webbrowser.open(f"http://127.0.0.1:{actual}/")
                instance.close()
                return None
            if time.monotonic() >= deadline:
                raise RuntimeError("Token Tracker is still starting or closing; try again shortly")
            time.sleep(0.25)
        # A replacement must take the old port, not silently move to the next one.
        while wait_port and port_in_use(port):
            if time.monotonic() >= deadline:
                raise RuntimeError("Token Tracker's previous dashboard has not closed yet")
            time.sleep(0.25)
        if not wait_port and tracker_answers(port):
            # a copy from before the instance lock existed is running there: open it, don't start a second one
            webbrowser.open(f"http://127.0.0.1:{port}/")
            instance.close()
            return None
        return instance
    except BaseException:
        instance.close()
        raise


def print_detected(db):
    sources.refresh(db)
    detected = {key: info for key, info in sources.meta().items() if info['detected']}
    print("\n감지된 AI:")
    for info in detected.values():
        print(f"  {info['name']}: {', '.join(info['models']) or '-'}")
    if not detected:
        print("  없음")


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def cmd_scan():
    db = DB()
    t = time.time()
    rep = scan_all(db)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print_detected(db)
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
    print_detected(db)
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
    ap.add_argument("--wait-port", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--refresh-shortcuts", action="store_true", help=argparse.SUPPRESS)  # after an exe update  # after self-update
    a = ap.parse_args()
    tls.install()

    if a.refresh_shortcuts:
        from tracker.shortcuts import refresh_existing
        return refresh_existing()
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

    from tracker import updater
    updater.cleanup_launcher(paths.APP_ROOT)
    port = a.port or paths.load_config().get("port", 8765)
    t0 = time.time()
    instance = claim_instance(port, a.wait_port)
    if instance is None:
        return
    engine, httpd = None, None
    try:
        if a.wait_port:  # the old copy has released both its lock and its dashboard
            updater.log(f"new copy v{updater.local_version()} started after {time.time() - t0:.1f}s wait")
            if os.name == "nt":
                import threading
                from tracker.shortcuts import refresh_existing
                threading.Thread(target=refresh_existing, daemon=True).start()
        engine = Engine()
        engine.start()

        use_widget = not a.dashboard
        if use_widget:
            try:
                from tracker.widget import Widget
            except ImportError:
                print("tkinter가 없어 대시보드 모드로 실행합니다")
                use_widget = False

        httpd, url = serve(engine, port=port, background=True)
        instance.publish(url)
        if use_widget:
            Widget(engine, url).run()
        else:
            print(f"대시보드: {url}   (끄려면 Ctrl+C)")
            if not a.no_browser:
                webbrowser.open(url)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        try:
            if engine is not None:
                engine.stop()
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
        finally:
            instance.close()


if __name__ == "__main__":
    main()
