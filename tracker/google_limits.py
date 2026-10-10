"""Read Google quota data without generating model requests or guessing token usage.

Gemini CLI protocol: google-gemini/gemini-cli, code_assist/server.ts.
Antigravity protocol is a local app interface and can change between releases.
Credentials stay local and are never included in returned results.
"""
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request

from . import paths
from .collectors import parse_ts


def _post(url, body, headers=None, context=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'token-tracker', **(headers or {})})
    with urllib.request.urlopen(request, timeout=8, context=context) as response:
        return json.load(response)


def quota_windows(data):
    """Normalize documented Gemini buckets and local Antigravity model quotas."""
    found = {}
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            quota = value.get('quotaInfo') or value
            fraction = quota.get('remainingFraction')
            if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and 0 <= fraction <= 1:
                model = value.get('modelId') or value.get('label') or value.get('modelOrAlias') or '모델 한도'
                if isinstance(model, dict):
                    model = model.get('model') or model.get('modelAlias') or '모델 한도'
                kind = value.get('tokenType') or ''
                key = f'{model}:{kind}'
                found[key] = {'key': key, 'label': str(model) + (' ' + kind if kind else ''),
                    'used_percent': 100 * (1 - fraction),
                    'resets_at': parse_ts(quota.get('resetTime') or value.get('resetTime'))}
            for item in value.values():
                if isinstance(item, (dict, list)):
                    # quotaInfo is handled with the parent model name above.
                    if item is not value.get('quotaInfo'):
                        walk(item)
    walk(data)
    return list(found.values())


def gemini_cli():
    credential = paths.gemini_root() / 'oauth_creds.json'
    if not credential.exists():
        return {'status': 'no_credentials', 'message': 'Gemini CLI에서 Google 로그인을 해주세요'}
    oauth = json.loads(credential.read_text(encoding='utf-8'))
    token = oauth.get('access_token')
    if not token or (oauth.get('expiry_date') and oauth['expiry_date'] / 1000 < time.time()):
        return {'status': 'expired', 'message': 'Gemini CLI를 실행해 로그인 토큰을 갱신해주세요'}
    headers = {'Authorization': 'Bearer ' + token}
    endpoint = 'https://cloudcode-pa.googleapis.com/v1internal:'
    loaded = _post(endpoint + 'loadCodeAssist', {'metadata': {'ideType': 'IDE_UNSPECIFIED'}}, headers)
    project = os.environ.get('GOOGLE_CLOUD_PROJECT') or loaded.get('cloudaicompanionProject')
    if isinstance(project, dict):
        project = project.get('id')
    body = {'project': project} if project else {}
    data = _post(endpoint + 'retrieveUserQuota', body, headers)
    windows = quota_windows(data)
    tier = loaded.get('currentTier') or {}
    return {'status': 'ok' if windows else 'no_data', 'windows': windows,
            'plan': tier.get('name') or tier.get('id'),
            'message': '' if windows else '서버 응답에 한도 정보가 없습니다'}


def _local_servers():
    if os.name == 'nt':
        command = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^language_server.*' } | Select-Object ProcessId,CommandLine,@{Name='Ports';Expression={@((Get-NetTCPConnection -OwningProcess $_.ProcessId -State Listen -ErrorAction SilentlyContinue).LocalPort)}} | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=12)
        procs = json.loads(result.stdout or '[]')
        procs = procs if isinstance(procs, list) else [procs]
    else:
        result = subprocess.run(['ps', '-axo', 'pid=,args='], capture_output=True, text=True, timeout=5)
        procs = [{'CommandLine': line, 'ProcessId': int(line.strip().split()[0])}
                 for line in result.stdout.splitlines() if 'language_server' in line]
    for proc in procs:
        command = proc.get('CommandLine') or ''
        if 'antigravity' not in command.lower():
            continue
        port = re.search(r'--(https_server_port|server_port)[=\s]+(\d+)', command)
        csrf = re.search(r'--csrf_token[=\s]+["\']?([^\s"\']+)', command)
        if port and csrf:
            ports = [int(port[2])] if int(port[2]) > 0 else proc.get('Ports') or []
            if isinstance(ports, dict):
                ports = ports.get('value') or []
            if isinstance(ports, (int, float)):
                ports = [ports]
            if not ports and os.name != 'nt':
                output = subprocess.run(['lsof', '-a', '-p', str(proc['ProcessId']), '-iTCP', '-sTCP:LISTEN', '-Fn'],
                    capture_output=True, text=True, timeout=5).stdout
                ports = [int(match) for match in re.findall(r'^n.*:(\d+)$', output, re.M)]
            for number in ports:
                yield ('https' if port[1] == 'https_server_port' else 'http'), int(number), csrf[1]


def antigravity():
    for scheme, port, csrf in _local_servers():
        # The app uses a self-signed loopback certificate. This context is only
        # passed to this explicitly constructed local address, never remote APIs.
        context = ssl._create_unverified_context() if scheme == 'https' else None
        try:
            data = _post(f'{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus',
                {}, {'X-Codeium-Csrf-Token': csrf, 'Connect-Protocol-Version': '1'}, context)
        except Exception:
            continue
        windows = quota_windows(data)
        status = data.get('userStatus') or {}
        plan = status.get('planStatus') or {}
        info = plan.get('planInfo') or {}
        return {'status': 'ok' if windows else 'no_data', 'windows': windows,
            'plan': info.get('planName'),
            'message': '모델별 한도는 앱 조회값입니다. 앱에서 토큰 수를 제공하지 않는 대화의 토큰 사용량은 집계할 수 없습니다' if windows else '앱 응답에 모델 한도 정보가 없습니다'}
    return {'status': 'no_data', 'message': 'Antigravity를 실행하면 모델별 한도를 조회합니다. 토큰 사용량은 앱 로그에 제공될 때만 집계합니다'}


def fetch(db, source, force=False):
    cached = db.get_kv(source + '_limits') or {}
    now = time.time()
    if not force and now - cached.get('checked_at', 0) < 300:
        return cached
    result = {'source': source, 'windows': cached.get('windows', []),
              'checked_at': now, 'fetched_at': cached.get('fetched_at')}
    try:
        result.update(gemini_cli() if source == 'gemini' else antigravity())
        if result.get('status') == 'ok':
            result['fetched_at'] = now
    except Exception:
        result.update(status='offline', message='한도 조회 실패. 앱 로그인·실행 상태를 확인해주세요')
    db.set_kv(source + '_limits', result)
    return result
