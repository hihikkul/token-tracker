"""Independent product selection; unavailable counters never mean zero usage."""
NAMES = {
    'claude': 'Claude Code', 'codex': 'Codex', 'chatgpt': 'ChatGPT',
    'gemini': 'Gemini CLI', 'antigravity': 'Antigravity', 'gemini_web': 'Gemini 웹',
}


def enabled(cfg=None):
    if cfg is None:
        from .paths import load_config
        cfg = load_config()
    selected = cfg.get('sources', {})
    return [key for key in NAMES if selected.get(key, True)]
