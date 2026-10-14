"""OFERTIX: sincronizacao de ofertas por product data feed autorizado da Awin.

A URL do feed deve ser obtida pelo proprio publisher no Create-a-Feed.
Nao coleta dados de paginas HTML nem cria links de afiliado por suposicao.
"""
import csv
import gzip
import hashlib
import io
import math
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin

AWIN_FEED_HOSTS = frozenset({"datafeed.api.productserve.com", "productdata.awin.com"})
AWIN_LINK_HOSTS = frozenset({"www.awin1.com", "awin1.com"})
MAX_DOWNLOAD = 12 * 1024 * 1024
MAX_CSV_BYTES = 30 * 1024 * 1024
MAX_ROWS = 12000
MAX_OFFERS = 4000
SYNC_LOCK = threading.Lock()
LAST_SYNC = {"state": "not_configured", "at": None, "scanned": 0, "published": 0, "error": None}


def _safe_url(value, hosts, *, length=2500):
    if not isinstance(value, str) or len(value) > length:
        return False
    try:
        parts = urlparse(value)
        return (parts.scheme == "https" and parts.hostname in hosts
                and parts.port in (None, 443) and not parts.username and not parts.password
                and not parts.fragment)
    except (TypeError, ValueError):
        return False


class _AwinRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        dest = urljoin(request.full_url, newurl)
        if not _safe_url(dest, AWIN_FEED_HOSTS):
            raise ValueError("Feed Awin redirecionou para destino nao permitido.")
        return super().redirect_request(request, fp, code, msg, headers, dest)


def fetch_awin_feed(feed_url):
    """Faz download com limites, sem revelar token/URL em erros."""
    if not _safe_url(feed_url, AWIN_FEED_HOSTS, length=5000):
        raise ValueError("Feed invalido. Use somente URL HTTPS do Create-a-Feed oficial Awin.")
    opener = urllib.request.build_opener(_AwinRedirects())
    req = urllib.request.Request(feed_url, headers={
        "Accept": "text/csv,application/gzip,application/octet-stream",
        "User-Agent": "OFERTIX-AffiliateFeed/14.0",
    })
    try:
        with opener.open(req, timeout=25) as response:
            raw = response.read(MAX_DOWNLOAD + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError("Awin nao autorizou ou nao entregou o feed (HTTP " + str(exc.code) + ").") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Nao foi possivel consultar o feed Awin.") from None
    if len(raw) > MAX_DOWNLOAD:
        raise ValueError("Feed comprimido muito grande. Crie um feed filtrado menor na Awin.")
    if raw.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                raw = gz.read(MAX_CSV_BYTES + 1)
        except (OSError, EOFError):
            raise ValueError("Feed gzip invalido.") from None
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("Feed acima de 30 MB. Filtre categorias e lojas no Create-a-Feed.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Feed CSV nao esta em UTF-8.") from None


def _money(raw):
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > 45:
        return None
    # CSVs Awin podem vir em 99.99, 99,99 ou 1.234,56.
    text = re.sub(r"^(R\$|BRL)\s*", "", text, flags=re.I).strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    if not re.fullmatch(r"\d{1,7}(?:\.\d{1,2})?", text):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return round(number, 2) if math.isfinite(number) and 0 < number <= 1000000 else None


def _valid_image(url):
    if not isinstance(url, str) or not 0 < len(url) <= 2000:
        return False
    try:
        p = urlparse(url)
        return (p.scheme == "https" and p.hostname not in ("localhost", "127.0.0.1")
                and bool(p.hostname) and "." in p.hostname and p.port in (None, 443)
                and not p.username and not p.password)
    except (ValueError, TypeError):
        return False


def _stock_available(row):
    stock = (row.get("in_stock") or row.get("stock_status") or "").strip().lower()
    if stock in ("0", "false", "no", "n", "out_of_stock", "out of stock", "sold_out", "unavailable"):
        return False
    if (row.get("is_for_sale") or "").strip().lower() in ("0", "false", "no"):
        return False
    return True


def parse_awin_csv(text, minimum_discount=10):
    """Retorna apenas ofertas rastreaveis. Nao usa rrp_price como preco anterior."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("Feed CSV vazio ou grande demais.")
    reader = csv.DictReader(io.StringIO(text), delimiter="," )
    required = {"aw_deep_link", "product_name", "search_price", "currency", "product_price_old"}
    if not reader.fieldnames or not required.issubset({name.strip() for name in reader.fieldnames}):
        raise ValueError(
            "Faltam colunas no feed. Selecione aw_deep_link, product_name, search_price, "
            "product_price_old, currency, merchant_id e aw_product_id no Create-a-Feed."
        )
    offers = {}
    scanned = 0
    for row in reader:
        scanned += 1
        if scanned > MAX_ROWS:
            raise ValueError("Feed muito grande. Filtre produtos no Create-a-Feed.")
        if not isinstance(row, dict) or None in row:
            continue
        currency = str(row.get("currency") or "").strip().upper()
        if currency != "BRL" or not _stock_available(row):
            continue
        price = _money(row.get("search_price"))
        old = _money(row.get("product_price_old"))
        if price is None or old is None or old <= price:
            continue
        discount = round((old - price) * 100.0 / old, 2)
        if discount < minimum_discount:
            continue
        affiliate = str(row.get("aw_deep_link") or "").strip()
        if not _safe_url(affiliate, AWIN_LINK_HOSTS):
            continue
        title = str(row.get("product_name") or "").strip()
        if len(title) < 5:
            continue
        title = title[:140]
        image = next((x.strip() for x in (
            row.get("aw_image_url"), row.get("merchant_image_url"), row.get("large_image"))
                      if isinstance(x, str) and _valid_image(x.strip())), None)
        if not image:
            continue
        merchant_id = str(row.get("merchant_id") or "").strip()
        merchant = str(row.get("merchant_name") or "").strip()[:70]
        pid = str(row.get("aw_product_id") or row.get("merchant_product_id") or "").strip()
        if not merchant_id.isdigit() or not pid or len(pid) > 100 or not merchant:
            continue
        oid = hashlib.sha256((merchant_id + ":" + pid).encode("utf-8")).hexdigest()[:32]
        category = str(row.get("merchant_category") or row.get("category_name") or "Ofertas").strip()[:70] or "Ofertas"
        offers[oid] = {
            "id": oid, "merchant": merchant, "title": title, "category": category,
            "price": price, "original_price": old, "discount_percent": discount,
            "affiliate_url": affiliate, "image": image
        }
        if len(offers) > MAX_OFFERS:
            raise ValueError("Muitas ofertas. Filtre lojas/categorias no Create-a-Feed.")
    if scanned == 0:
        raise ValueError("O feed esta vazio, nenhuma atualizacao foi realizada.")
    return {"scanned": scanned, "offers": list(offers.values())}


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS affiliate_feed_offers(
        id TEXT PRIMARY KEY, merchant TEXT NOT NULL, title TEXT NOT NULL,
        category TEXT NOT NULL, price REAL NOT NULL, original_price REAL NOT NULL,
        discount_percent REAL NOT NULL, affiliate_url TEXT NOT NULL,
        image TEXT NOT NULL, checked_at TEXT NOT NULL, expires_at TEXT NOT NULL
    )""")


def import_parsed(conn, parsed, *, at=None, ttl_hours=24):
    """Upsert atomico em transacao do chamador; itens ausentes expiram so por TTL."""
    if not isinstance(parsed, dict) or parsed.get("scanned", 0) <= 0:
        raise ValueError("Feed nao validado.")
    at = at or datetime.now(timezone.utc)
    current = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    expiry = (at + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in parsed["offers"]:
        conn.execute("""INSERT INTO affiliate_feed_offers
                    (id,merchant,title,category,price,original_price,discount_percent,
                     affiliate_url,image,checked_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                    merchant=excluded.merchant,title=excluded.title,category=excluded.category,
                    price=excluded.price,original_price=excluded.original_price,
                    discount_percent=excluded.discount_percent,affiliate_url=excluded.affiliate_url,
                    image=excluded.image,checked_at=excluded.checked_at,expires_at=excluded.expires_at""",
                    (row["id"],row["merchant"],row["title"],row["category"],row["price"],
                     row["original_price"],row["discount_percent"],row["affiliate_url"],
                     row["image"],current,expiry))
    return len(parsed["offers"])


def visible_offers(conn):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = [dict(x) for x in conn.execute(
        """SELECT id,merchant,title,category,price,original_price,discount_percent,
                  affiliate_url,image,checked_at,expires_at
           FROM affiliate_feed_offers WHERE expires_at > ?
           ORDER BY discount_percent DESC LIMIT 300""", (now,)
    )]
    for row in results:
        row["pricing_source"] = "affiliate_feed"
        row["verified_discount"] = False
        row["network"] = "Awin"
    return results


def run_sync(db_context, feed_url, minimum_discount=10):
    """Sincroniza uma fonte autorizada, sem parametros fornecidos pelo navegador."""
    if not feed_url:
        LAST_SYNC.update(state="not_configured", error=None)
        return {"state": "not_configured", "message": "Configure OFERTIX_AWIN_FEED_URL no servidor."}
    if not SYNC_LOCK.acquire(blocking=False):
        return {"state": "already_running"}
    try:
        LAST_SYNC.update(state="running", error=None)
        content = fetch_awin_feed(feed_url)
        parsed = parse_awin_csv(content, minimum_discount=minimum_discount)
        with db_context() as conn:
            published = import_parsed(conn, parsed)
        LAST_SYNC.update(state="ok", at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         scanned=parsed["scanned"],published=published,error=None)
        return {"state":"ok","scanned":parsed["scanned"],"published":published}
    except (ValueError, OSError, csv.Error):
        LAST_SYNC.update(state="error", error="Falha ao importar feed. Verifique autorizacao, colunas e limites do CSV.")
        return {"state": "error", "message": LAST_SYNC["error"]}
    finally:
        SYNC_LOCK.release()
