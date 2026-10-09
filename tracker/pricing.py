"""API-equivalent cost estimate from prices.json (longest matching pattern wins)."""
import json
from functools import lru_cache

from .paths import APP_DIR, data_dir


@lru_cache(maxsize=1)
def _table():
    table = {}
    for p in (APP_DIR / "prices.json", data_dir() / "prices.json"):  # user copy overrides
        if p.exists():
            try:
                table.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items()
                              if not k.startswith("_")})
            except (OSError, ValueError):
                pass
    return table


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


def cost(model, input=0, output=0, cache_write=0, cache_read=0, **_):
    p = price_for(model)
    if not p:
        return None
    return (input * p["input"] + output * p["output"]
            + cache_write * p.get("cache_write", 0) + cache_read * p.get("cache_read", 0)) / 1e6
