"""Independent product selection; unavailable counters never mean zero usage."""
NAMES = {
    'claude': 'Claude Code', 'codex': 'Codex', 'chatgpt': 'ChatGPT',
    'gemini': 'Gemini CLI', 'antigravity': 'Antigravity', 'gemini_web': 'Gemini Web',
}


def connected(limit):
    """A usable quota snapshot; cached windows remain useful during an outage."""
    if limit.get('status') in ('no_credentials', 'expired', 'auth', 'unsupported', 'no_data'):
        return False
    return bool(limit.get('windows') or any(account.get('windows')
                for account in limit.get('accounts', [])))


def enabled(cfg=None):
    if cfg is None:
        from .paths import load_config
        cfg = load_config()
    selected = cfg.get('sources', {})
    return [key for key in NAMES if selected.get(key, True)]
