"""API-equivalent cost estimates from curated prices and catalog fallbacks."""
import json
import math
import threading
import time
import urllib.request
from functools import lru_cache

from . import tls, updater
from .paths import APP_DIR, _write_atomic, data_dir

_REMOTE_URL = f"https://raw.githubusercontent.com/{updater.REPO}/main/prices.json"
_LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
_REMOTE_FILE = "prices.remote.json"
_AUTO_FILE = "prices.auto.json"
_FIRST_PARTY = ("openai", "anthropic", "deepseek", "gemini", "xai", "mistral", "moonshot",
                "zai", "minimax", "cohere")
_RESELLERS = {"azure", "azure_ai", "bedrock", "vertex_ai", "openrouter", "dashscope",
              "qwencloud", "together", "together_ai", "fireworks", "fireworks_ai"}
_FIELDS = ("input", "output", "cache_read", "cache_write")
_LITELLM_FIELDS = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost",
                   "cache_creation_input_token_cost")
_OPENROUTER_FIELDS = ("prompt", "completion", "input_cache_read", "input_cache_write")
_refresh_lock = threading.Lock()

# DeepSeek's UTC weekday schedule; Chinese public holidays are not accounted for.
PEAK_SQL = """(CASE WHEN CAST(strftime('%w',ts,'unixepoch') AS INT) BETWEEN 1 AND 5
    AND (CAST(strftime('%H',ts,'unixepoch') AS INT) BETWEEN 1 AND 3
         OR CAST(strftime('%H',ts,'unixepoch') AS INT) BETWEEN 6 AND 9)
    THEN 1 ELSE 0 END)"""
_cache_lock = threading.Lock()
generation = 0  # dashboard aggregates include the rates in effect when they were built


def _validate(row):
    if not isinstance(row, dict):
        raise ValueError("price row must be an object")
    for key in ("input", "output", "cache_read", "cache_write"):
        if key not in row and key in ("cache_read", "cache_write"):
            continue
        value = row.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value < 0 or not math.isfinite(value)):
            raise ValueError(f"{key} must be a finite, non-negative number")
    if "peak" in row:
        if not isinstance(row["peak"], dict) or "peak" in row["peak"]:
            raise ValueError("peak must be a price object without nested peak rates")
        _validate(row["peak"])


def _object(rows):
    if not isinstance(rows, dict):
        raise ValueError("price table must be an object")
    return rows


def _read_object(path, problems):
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError) as exc:
        problems.append(f"{path}: {exc}")
    return {}


def _validated_rows(rows, path=None, problems=None):
    valid = {}
    for pattern, row in rows.items():
        if pattern.startswith("_"):
            continue
        try:
            if not pattern:
                raise ValueError("model pattern must not be empty")
            _validate(row)
        except (ValueError, OverflowError) as exc:
            if problems is not None:
                problems.append(f"{path}: {pattern}: {exc}")
            continue
        valid[pattern.lower()] = row
    return valid


@lru_cache(maxsize=1)
def _table():
    table, problems = {}, []
    root = data_dir()
    for path in (APP_DIR / "prices.json", root / _REMOTE_FILE, root / "prices.json"):
        table.update(_validated_rows(_read_object(path, problems), path, problems))
    return table, problems


@lru_cache(maxsize=1)
def _auto_table():
    path = data_dir() / _AUTO_FILE
    problems = []
    return _validated_rows(_read_object(path, problems), path, problems), problems


def errors():
    """Problems in the loaded price files, without exposing the mutable diagnostics lists."""
    with _cache_lock:
        return _table()[1] + _auto_table()[1]


def _curated_price(model, table):
    pattern = max((pat for pat in table if pat in model), key=len, default=None)
    return table[pattern] if pattern is not None else None


@lru_cache(maxsize=512)
def _price_for(model):
    curated = _curated_price(model, _table()[0])
    return curated if curated is not None else _auto_table()[0].get(model)


def price_for(model):
    if not model:
        return None
    with _cache_lock:
        return _price_for(model.lower())


price_for.cache_clear = _price_for.cache_clear


def reload():
    """Discard cached files and lookups after a price file changes."""
    # A concurrent lookup must not repopulate the caches with pre-update results.
    global generation
    with _cache_lock:
        generation += 1
        _table.cache_clear()
        _auto_table.cache_clear()
        price_for.cache_clear()


def _fetch(url, limit):
    request = urllib.request.Request(url, headers=updater.UA)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls.context()))
    with opener.open(request, timeout=15) as response:
        body = response.read(limit)
    if len(body) == limit:
        raise ValueError("price response reached the size limit")
    return body


def _write_changed(path, rows):
    text = json.dumps(rows, ensure_ascii=True, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (FileNotFoundError, UnicodeError):
        pass
    _write_atomic(path, text)
    return True


def _begin_check(db, key, now, interval):
    last = db.get_kv(key)
    if last is not None and now - last < interval:
        return False
    db.set_kv(key, now)  # failed attempts count toward the throttle too
    return True


def _refresh_remote(db, now):
    if not _begin_check(db, "prices_remote_checked_at", now, 24 * 3600):
        return False
    rows = _object(json.loads(_fetch(_REMOTE_URL, 512 * 1024)))
    return _write_changed(data_dir() / _REMOTE_FILE, _validated_rows(rows))


def _catalog_price(row, fields, source):
    if not isinstance(row, dict):
        return None
    price = {"source": source}
    try:
        for i, (field, key) in enumerate(zip(_FIELDS, fields)):
            value = row.get(key, 0 if i >= 2 else None)
            if isinstance(value, bool) or (source == "litellm" and not isinstance(value, (int, float))):
                return None
            value = float(value) * 1e6
            if not math.isfinite(value) or value < 0:
                return None
            price[field] = round(value, 6)
    except (TypeError, ValueError, OverflowError):
        return None
    return price


def _catalog_matches(entries, models, fields, source):
    matches, ranks = {}, {}
    for key, row in entries:
        if not isinstance(key, str):
            continue
        key = key.lower()
        maker, sep, _ = key.partition("/")
        tail = key.rsplit("/", 1)[-1]
        if source == "litellm":
            if sep and maker in _RESELLERS:
                continue
            suffix_rank = _FIRST_PARTY.index(maker) + 1 if sep and maker in _FIRST_PARTY else None
        else:
            suffix_rank = 1
        for model, rank in ((key, 0), (tail, suffix_rank)):
            if rank is None or model not in models or rank >= ranks.get(model, math.inf):
                continue
            price = _catalog_price(row, fields, source)
            if price is not None:
                matches[model], ranks[model] = price, rank
    return matches


def _catalog_prices(models):
    matches, sources, loaded = {}, [], False
    try:
        rows = _object(json.loads(_fetch(_LITELLM_URL, 16 * 1024 * 1024)))
        loaded = True
        matches = _catalog_matches(rows.items(), models, _LITELLM_FIELDS, "litellm")
        if matches:
            sources.append("litellm")
    except Exception:
        pass  # OpenRouter can still price these models
    remaining = models - matches.keys()
    if remaining:
        try:
            rows = _object(json.loads(_fetch(_OPENROUTER_URL, 8 * 1024 * 1024)))["data"]
            if not isinstance(rows, list):
                raise ValueError("OpenRouter data must be an array")
            loaded = True
            entries = ((row.get("id"), row.get("pricing")) for row in rows if isinstance(row, dict))
            fallback = _catalog_matches(entries, remaining, _OPENROUTER_FIELDS, "openrouter")
            if fallback:
                matches.update(fallback)
                sources.append("openrouter")
        except Exception:
            pass
    return matches, sources, loaded


def _refresh_auto(db, now):
    with _cache_lock:
        curated = _table()[0]
    models = {row["model"].lower() for row in db.query("SELECT DISTINCT model FROM records")
              if isinstance(row["model"], str) and row["model"] and not row["model"].startswith("_")}
    unknown = {model for model in models if _curated_price(model, curated) is None}
    path = data_dir() / _AUTO_FILE
    previous = _read_object(path, [])
    old_rows = _validated_rows(previous)
    rows = {model: row for model, row in old_rows.items() if _curated_price(model, curated) is None}
    meta = previous.get("_meta")
    meta = dict(meta) if isinstance(meta, dict) else {"fetched_at": 0, "sources": []}
    tried = meta.get("tried") if isinstance(meta.get("tried"), dict) else {}
    tried = {m: t for m, t in tried.items() if m in unknown and isinstance(t, (int, float))}
    # A model is looked up once, then again weekly: the catalogs are megabytes, and some models
    # (codex-auto-review) have no public price, so they would otherwise be fetched every 6 hours.
    due = {m for m in unknown if m not in tried or now - tried[m] >= 7 * 86400}
    matches = {}
    if due and _begin_check(db, "prices_auto_checked_at", now, 6 * 3600):
        matches, sources, loaded = _catalog_prices(due)
        if loaded:  # offline: try again after the 6-hour throttle, not in a week
            tried.update({m: now for m in due})
        if matches:
            rows.update(matches)
            meta.update(fetched_at=now, sources=sources)
    changed = bool(matches) or rows != old_rows or tried != meta.get("tried", {})
    meta["tried"] = tried
    return changed and _write_changed(path, {"_meta": meta, **rows})


def refresh(db, now=None):
    """Update prices in the background; keep the last usable prices on errors."""
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        now = time.time() if now is None else now
        for update in (_refresh_remote, _refresh_auto):
            try:
                if update(db, now):
                    reload()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _refresh_lock.release()


def cost(model, input=0, output=0, cache_write=0, cache_read=0, peak=None, **_):
    try:
        p = price_for(model)
        if not p:
            return None
        if peak and "peak" in p:
            p = p["peak"]
        amount = (input * p["input"] + output * p["output"]
                  + cache_write * p.get("cache_write", 0) + cache_read * p.get("cache_read", 0)) / 1e6
        return amount if math.isfinite(amount) else None
    except Exception:
        # Estimation must not interrupt collection, even with bad models, counts or price files.
        return None
