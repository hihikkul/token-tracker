"""API-equivalent cost estimate from prices.json (longest matching pattern wins)."""
import json
import math
from functools import lru_cache

from .paths import APP_DIR, data_dir

# DeepSeek's UTC weekday schedule; Chinese public holidays are not accounted for.
PEAK_SQL = """(CASE WHEN CAST(strftime('%w',ts,'unixepoch') AS INT) BETWEEN 1 AND 5
    AND (CAST(strftime('%H',ts,'unixepoch') AS INT) BETWEEN 1 AND 3
         OR CAST(strftime('%H',ts,'unixepoch') AS INT) BETWEEN 6 AND 9)
    THEN 1 ELSE 0 END)"""
_load_errors = []


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


@lru_cache(maxsize=1)
def _table():
    table = {}
    _load_errors.clear()
    for p in (APP_DIR / "prices.json", data_dir() / "prices.json"):  # user copy overrides
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(rows, dict):
                raise ValueError("price table must be an object")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            _load_errors.append(f"{p}: {exc}")
            continue
        for pattern, row in rows.items():
            if pattern.startswith("_"):
                continue
            try:
                if not pattern:
                    raise ValueError("model pattern must not be empty")
                _validate(row)
            except (ValueError, OverflowError) as exc:
                _load_errors.append(f"{p}: {pattern}: {exc}")
                continue
            table[pattern.lower()] = row
    return table


def errors():
    """Problems in the loaded price files, without exposing the mutable diagnostics list."""
    _table()
    return list(_load_errors)


@lru_cache(maxsize=512)
def price_for(model):
    if not model:
        return None
    m = model.lower()
    best = None
    for pat, price in _table().items():
        if pat in m and (best is None or len(pat) > len(best[0])):
            best = (pat, price)
    return best[1] if best else None


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
