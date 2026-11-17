"""Which products are tracked: the built-in apps, plus AI providers found in the logs.

Unavailable counters never mean zero usage. NAMES is live: when a model from a provider that is
not built in (DeepSeek, Qwen, Grok...) shows up in a log, refresh() adds that provider here, and
everything that lists products (settings, dashboard, exports) shows it without a code change.
Iterate over names() rather than NAMES itself, since a provider can be added at any moment.
"""
import re
import threading

BUILTIN = {
    'claude': 'Claude Code', 'codex': 'Codex', 'chatgpt': 'ChatGPT',
    'gemini': 'Gemini CLI', 'antigravity': 'Antigravity', 'gemini_web': 'Gemini Web',
}
NAMES = dict(BUILTIN)  # grows in place as providers are detected
OTHER = 'other'  # a model whose provider cannot be told from its name

# Known providers, matched against the lowercased model name in this order. Anything else is
# named after the organisation in an 'org/model' name, or after the model's first word.
VENDORS = (
    ('deepseek', 'DeepSeek', r'deepseek'),
    ('qwen', 'Qwen', r'qwen|qwq'),
    ('kimi', 'Kimi', r'kimi|moonshot'),
    ('glm', 'GLM', r'(^|[/_.-])glm|chatglm|zhipu'),
    ('grok', 'Grok', r'grok|x-ai/|xai/'),
    ('mistral', 'Mistral', r'mistral|codestral|devstral|magistral|pixtral'),
    ('minimax', 'MiniMax', r'minimax'),
    ('llama', 'Llama', r'llama'),
    ('gemma', 'Gemma', r'gemma'),
    ('cohere', 'Cohere', r'cohere|command-r|command-a'),
    ('nova', 'Amazon Nova', r'amazon[./-]nova|(^|/)nova-'),
    ('phi', 'Phi', r'(^|/)phi-'),
    ('openai_api', 'OpenAI API', r'gpt-oss|(^|/)openai/'),  # API key or router, not a ChatGPT plan
)
_VENDOR_NAMES = {key: name for key, name, _ in VENDORS}
_VENDOR_RE = [(key, re.compile(pat)) for key, _, pat in VENDORS]
# path segments that name a router or cloud rather than the model's maker
_ROUTERS = {'openrouter', 'together', 'togethercomputer', 'fireworks', 'accounts', 'models', 'groq',
            'bedrock', 'vertex_ai', 'vertex', 'azure', 'ollama', 'lmstudio', 'hf', 'huggingface',
            'nvidia', 'cerebras', 'sambanova', 'deepinfra', 'novita', 'siliconflow', 'litellm'}
_GENERIC = {'unknown', 'default', 'model', 'models', 'test', 'local', 'custom', 'auto', 'chat',
            'base', 'instruct', 'latest', 'api', 'synthetic', 'other'}

_lock = threading.Lock()
_detected = {}  # key -> {"name", "first_seen", "models"}; saved in the kv table


def vendor_source(model):
    """Source key for a model that no built-in app accounts for: a known provider, else the
    organisation or first word of its name ('acme/coder-2' -> 'acme'), else 'other'."""
    m = (model or '').strip().lower()
    if not m:
        return OTHER
    for key, pat in _VENDOR_RE:
        if pat.search(m):
            return key
    parts = [p for p in m.split('/') if p]
    orgs = [p for p in parts[:-1] if p not in _ROUTERS]
    word = orgs[0] if orgs else (re.match(r'[a-z][a-z0-9]*', parts[-1]) or [''])[0]
    key = re.sub(r'[^a-z0-9]+', '_', word).strip('_')[:24]
    if len(key) < 3 or key in _GENERIC:
        return OTHER
    return key + '_api' if key in BUILTIN else key


def display_name(key):
    if key in BUILTIN:
        return BUILTIN[key]
    if key == OTHER:
        return 'Other models'
    return _VENDOR_NAMES.get(key) or key.replace('_', ' ').title()


def is_subscription(key):
    """Built-in apps run on a subscription plan; detected providers are paid per token (or free
    when run locally), so their API-equivalent cost is real spend, not a subscription's value."""
    return key in BUILTIN


def names():
    with _lock:
        return dict(NAMES)


def meta():
    """Every product with what the dashboard needs to show it."""
    with _lock:
        out = {}
        for key, name in NAMES.items():
            d = _detected.get(key) or {}
            out[key] = {'name': name, 'kind': 'subscription' if key in BUILTIN else 'payg',
                        'detected': key not in BUILTIN, 'first_seen': d.get('first_seen'),
                        'models': list(d.get('models') or [])}
        return out


def _register(key, info):
    _detected[key] = info
    NAMES[key] = info.get('name') or display_name(key)


def load(db):
    """Providers detected in earlier runs, so they are listed before the first scan ends."""
    saved = db.get_kv('detected_sources') or {}
    with _lock:
        for key, info in saved.items():
            if key not in BUILTIN and isinstance(info, dict):
                _register(key, info)


def refresh(db):
    """Register providers that now appear in the records. Returns the keys seen for the first time."""
    load(db)
    placeholders = ','.join('?' * len(BUILTIN))
    rows = db.query(f"SELECT source, MIN(ts) AS first_seen, GROUP_CONCAT(DISTINCT model) AS models "
                    f"FROM records WHERE source NOT IN ({placeholders}) GROUP BY source", tuple(BUILTIN))
    new, changed = [], False
    with _lock:
        for r in rows:
            key = r['source']
            if not key:
                continue
            models = sorted({m for m in (r['models'] or '').split(',') if m})[:20]
            info = _detected.get(key)
            if info is None:
                new.append(key)
                info = {'name': display_name(key), 'first_seen': r['first_seen'], 'models': models}
                changed = True
            elif info.get('models') != models:
                info = dict(info, models=models)
                changed = True
            _register(key, info)
        snapshot = dict(_detected)
    if changed:
        db.set_kv('detected_sources', snapshot)
    return new


def connected(limit):
    """A usable quota snapshot; cached windows remain useful during an outage."""
    if limit.get('status') in ('no_credentials', 'expired', 'auth', 'relogin', 'unsupported', 'no_data'):
        return False
    return bool(limit.get('windows') or any(account.get('windows')
                for account in limit.get('accounts', [])))


def enabled(cfg=None):
    """Selected products; one detected later is selected until the user turns it off."""
    if cfg is None:
        from .paths import load_config
        cfg = load_config()
    selected = cfg.get('sources', {})
    return [key for key in names() if selected.get(key, True)]
