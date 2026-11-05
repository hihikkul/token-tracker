"""One-time deploy of the ranking server to your Cloudflare account (free plan is enough).

    deploy_server.bat   (Windows)     ./deploy_server.command   (macOS)

Needs Node.js (https://nodejs.org, LTS). A browser window opens once to log in to
Cloudflare. Running it again just redeploys the latest server code.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
NAME = "token-tracker-rank"
WIN = os.name == "nt"


def wr(*args, capture=False, cwd=HERE):
    cmd = ["npx", "--yes", "wrangler@4", *args]
    print("\n>", " ".join(cmd[2:]))
    r = subprocess.run(cmd, cwd=cwd, shell=WIN, text=True, encoding="utf-8", errors="replace",
                       capture_output=capture)
    if capture:
        if r.returncode:
            print(r.stdout[-2000:], r.stderr[-2000:])
        return r
    return r


def main():
    if not shutil.which("npx"):
        print("Node.js가 필요해요. https://nodejs.org 에서 LTS 버전을 설치한 뒤 다시 실행해 주세요.")
        return 1
    if wr("whoami", capture=True).stdout.find("You are logged in") < 0:
        print("브라우저에서 Cloudflare 로그인 후 'Allow'를 눌러 주세요.")
        if wr("login").returncode:
            return 1

    def find_db():
        r = wr("d1", "list", "--json", capture=True)
        try:
            return next((d["uuid"] for d in json.loads(r.stdout) if d.get("name") == NAME), None)
        except ValueError:
            return None
    db_id = find_db()
    if not db_id:
        wr("d1", "create", NAME)
        db_id = find_db()
    if not db_id:
        print("D1 데이터베이스를 만들지 못했어요.")
        return 1

    for toml in (HERE / "wrangler.toml", HERE / "pages" / "wrangler.toml"):
        text = toml.read_text(encoding="utf-8")
        toml.write_text(re.sub(r'database_id = "[^"]*"', f'database_id = "{db_id}"', text), encoding="utf-8")

    if wr("d1", "execute", NAME, "--remote", "--file=schema.sql", "--yes").returncode:
        return 1
    if wr('d1', 'migrations', 'apply', NAME, '--remote').returncode:
        return 1
    r = wr("deploy", capture=True)  # the older address, which older copies of the app still use
    print(r.stdout[-1500:])
    if r.returncode:
        print("Worker를 배포하지 못했어요. 위 메시지를 확인해 주세요.")
        return 1

    def pages_url():
        r = wr("pages", "project", "list", "--json", capture=True)
        try:
            for p in json.loads(r.stdout[r.stdout.index("["):]):
                if p.get("Project Name") == NAME:
                    return "https://" + p["Project Domains"].split(",")[0].strip()
        except (ValueError, KeyError, AttributeError):
            pass
        return None
    if not pages_url():
        # --force: without it, newer wrangler runs `wrangler deploy` instead of creating a Pages project,
        # which from this folder's config would redeploy the Worker, on the slow *.workers.dev route.
        wr("pages", "project", "create", NAME, "--production-branch", "main", "--force", cwd=HERE / "pages")
    r = wr("pages", "deploy", "dist", "--project-name", NAME, "--branch", "main", "--commit-dirty=true",
           capture=True, cwd=HERE / "pages")
    print(r.stdout[-1500:])
    url = pages_url()
    if r.returncode or not url:
        print("배포 주소를 찾지 못했어요. 위 메시지를 확인해 주세요.")
        return 1

    from tracker import paths  # remember it for this PC right away
    cfg = paths.load_config()
    cfg.setdefault("ranking", {})["server"] = url
    paths.save_config(cfg)
    print("\n" + "=" * 60)
    print(" 랭킹 서버 준비 완료:", url)
    print(" 이 PC의 트래커에는 바로 적용했어요 (트래커를 한 번 다시 켜 주세요).")
    print(" 친구들도 쓰게 하려면 이 주소를 Claude에게 알려 주세요.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
