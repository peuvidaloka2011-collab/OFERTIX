"""Catálogo automático de afiliados: leitura pública segura, sem tokens no Render.

O GitHub Actions atualiza o branch ofertix-catalog. O Render consulta apenas
o catálogo público, não precisa acessar a chave do feed nem manter SQLite.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

CATALOG_URL = (
    "https://raw.githubusercontent.com/peuvidaloka2011-collab/"
    "OFERTIX/ofertix-catalog/catalog_awin.json"
)
MAX_CATALOG_BYTES = 5 * 1024 * 1024
CACHE_SECONDS = 300
_CACHE = {"at": 0.0, "offers": [], "partner_products": [], "expires_at": None}
_LOCK = threading.Lock()
_ALLOWED_AFFILIATE_HOSTS = frozenset({"www.awin1.com", "awin1.com"})


def _parse_utc(raw):
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _secure_url(raw, hosts=None):
    if not isinstance(raw, str) or len(raw) > 2200:
        return False
    try:
        parsed = urlparse(raw)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and (hosts is None or parsed.hostname in hosts)
                and parsed.port in (None, 443)
                and not parsed.username and not parsed.password
                and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def read_catalog(payload, min_discount=10, *, now=None):
    """Retorna somente produtos válidos e atuais de catálogo Awin aprovado."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return []
    now = now or datetime.now(timezone.utc)
    updated = _parse_utc(payload.get("generated_at"))
    expires = _parse_utc(payload.get("expires_at"))
    if updated is None or expires is None or not updated <= now < expires:
        return []
    rows = payload.get("products")
    if not isinstance(rows, list) or len(rows) > 4000:
        return []
    result = []
    ids = set()
    for p in rows:
        if not isinstance(p, dict):
            continue
        oid = p.get("id")
        if not isinstance(oid, str) or not oid or len(oid) > 80 or oid in ids:
            continue
        title = p.get("title")
        image = p.get("image")
        merchant = p.get("merchant")
        if not isinstance(title, str) or len(title.strip()) < 5 or len(title) > 180:
            continue
        if not isinstance(merchant, str) or not 0 < len(merchant.strip()) <= 90:
            continue
        if not _secure_url(p.get("affiliate_url"), _ALLOWED_AFFILIATE_HOSTS):
            continue
        if not _secure_url(image):
            continue
        try:
            price = float(p["price"])
            old_price = float(p["original_price"])
            discount = float(p["discount_percent"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if not all(map(math.isfinite, (price, old_price, discount))):
            continue
        if not 0 < price < old_price <= 1000000:
            continue
        real_discount = (old_price - price) * 100 / old_price
        if discount < min_discount or abs(discount - real_discount) > 0.6:
            continue
        ids.add(oid)
        result.append({
            "id": "awin_" + oid, "title": title.strip(),
            "merchant": merchant.strip(),
            "category": str(p.get("category") or "Ofertas")[:70],
            "price": round(price, 2),
            "original_price": round(old_price, 2),
            "discount_percent": round(real_discount, 2),
            "affiliate_url": p["affiliate_url"],
            "image": image,
            "network": "Awin",
            "pricing_source": "affiliate_feed",
            "verified_discount": False,
        })
    return sorted(result, key=lambda p: p["discount_percent"], reverse=True)[:300]


def get_offers(min_discount=10):
    """Cacheia leitura da lista no GitHub por 5 min. Nunca envia segredo ao browser."""
    with _LOCK:
        now_mono = time.monotonic()
        valid_cache = (_CACHE["expires_at"] is not None and
                       datetime.now(timezone.utc) < _CACHE["expires_at"])
        if _CACHE["at"] and now_mono - _CACHE["at"] < CACHE_SECONDS:
            return [x for x in _CACHE["offers"] if x["discount_percent"] >= min_discount] if valid_cache else []
        offers = []
        partner_products = []
        expires_at = None
        try:
            req = urllib.request.Request(
                CATALOG_URL,
                headers={"Accept": "application/json", "User-Agent": "OFERTIX/17"},
            )
            with urllib.request.urlopen(req, timeout=7) as response:
                raw = response.read(MAX_CATALOG_BYTES + 1)
            if len(raw) > MAX_CATALOG_BYTES:
                raise ValueError("Catalogo excedeu o limite.")
            payload = json.loads(raw.decode("utf-8"))
            expires_at = _parse_utc(payload.get("expires_at")) if isinstance(payload, dict) else None
            offers = read_catalog(payload, min_discount=1)
            partner_products = read_partner_catalog(payload)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError, UnicodeError, json.JSONDecodeError):
            # Falha de rede: retorna cache anterior, se ainda nao estiver expirado;
            # a validade e avaliada a cada nova consulta periodica.
            offers = _CACHE["offers"] if valid_cache else []
            partner_products = _CACHE["partner_products"] if valid_cache else []
            expires_at = _CACHE["expires_at"] if valid_cache else None
        _CACHE.update(at=now_mono, offers=offers, partner_products=partner_products, expires_at=expires_at)
        if expires_at is None or datetime.now(timezone.utc) >= expires_at:
            return []
        return [x for x in offers if x["discount_percent"] >= min_discount]


def read_partner_catalog(payload, *, now=None):
    """Produtos com preço real, mas SEM desconto confirmado pelo feed."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return []
    now = now or datetime.now(timezone.utc)
    start = _parse_utc(payload.get("generated_at"))
    end = _parse_utc(payload.get("expires_at"))
    if start is None or end is None or not start <= now < end:
        return []
    rows = payload.get("partner_products", [])
    if not isinstance(rows, list) or len(rows) > 2000:
        return []
    items = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        oid = item.get("id")
        title = item.get("title")
        merchant = item.get("merchant")
        if not isinstance(oid, str) or not oid or len(oid)>80 or oid in seen:
            continue
        if not isinstance(title,str) or not 5<=len(title)<=180:
            continue
        if not isinstance(merchant,str) or not 0<len(merchant)<=90:
            continue
        try:
            price=float(item["price"])
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
        if not math.isfinite(price) or not 0<price<=1000000:
            continue
        if not _secure_url(item.get("affiliate_url"),_ALLOWED_AFFILIATE_HOSTS):
            continue
        if not _secure_url(item.get("image")):
            continue
        seen.add(oid)
        items.append({
            "id":"awin_"+oid,"title":title,"merchant":merchant,
            "category":str(item.get("category") or "Produtos parceiros")[:70],
            "image":item["image"],"price":round(price,2),
            "affiliate_url":item["affiliate_url"],
            "pricing_source":"affiliate_catalog","network":"Awin",
            "verified_discount":False,
        })
    return items


def get_partner_products():
    """Atualiza cache junto com as promoções, sem chamadas extras ao GitHub."""
    get_offers(min_discount=1)
    with _LOCK:
        if _CACHE["expires_at"] is None or datetime.now(timezone.utc)>=_CACHE["expires_at"]:
            return []
        return list(_CACHE["partner_products"])
