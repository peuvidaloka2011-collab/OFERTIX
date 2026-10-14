"""OFERTIX: vitrine de promocoes verificadas pela API oficial do Mercado Livre."""
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
DB = Path(os.getenv("OFERTIX_DB_PATH", str(BASE / "ofertix.db")))
TOKEN = os.getenv("ML_ACCESS_TOKEN", "").strip()
ADMIN_KEY = os.getenv("OFERTIX_ADMIN_KEY", "").strip()
PROMO_MIN = max(1, min(80, int(os.getenv("OFERTIX_MIN_DISCOUNT", "10"))))
FRESH_HOURS = max(1, min(72, int(os.getenv("OFERTIX_FRESH_HOURS", "12"))))
INTERVAL_HOURS = max(1, min(24, int(os.getenv("OFERTIX_SYNC_HOURS", "6"))))
CATEGORIES = [c.strip().upper() for c in os.getenv("OFERTIX_CATEGORIES", "").split(",") if re.fullmatch(r"MLB\d+", c.strip().upper())]
IMPORT_LOCK = threading.Lock()
LAST_SYNC = {"time": None, "status": "aguardando configuracao", "details": []}
VALID_ID = re.compile(r"MLB\d+")


@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            price REAL,
            image TEXT,
            category TEXT,
            ranking INTEGER,
            affiliate_url TEXT,
            original_url TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        for name, definition in {
            "original_price": "REAL",
            "discount_percent": "REAL DEFAULT 0",
            "is_promo": "INTEGER DEFAULT 0",
            "promo_checked_at": "TEXT",
            "currency": "TEXT DEFAULT 'BRL'",
        }.items():
            if name not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN " + name + " " + definition)


def ml_api(path):
    if not TOKEN:
        raise RuntimeError("ML_ACCESS_TOKEN nao configurado; configure OAuth antes de importar.")
    req = urllib.request.Request(
        "https://api.mercadolibre.com" + path,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json", "User-Agent": "OFERTIX/2.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def allowed_affiliate_link(value):
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and host in {
                "meli.la", "www.meli.la", "mercadolivre.com", "www.mercadolivre.com",
                "mercadolivre.com.br", "www.mercadolivre.com.br",
                "mercadolibre.com", "www.mercadolibre.com",
            }
        )
    except (TypeError, ValueError):
        return False


def positive_amount(number):
    if isinstance(number, bool):
        return None
    try:
        value = float(number)
        if 0 < value < 1e10 and value == value:
            return value
    except (TypeError, ValueError):
        pass
    return None


def verified_promotion(item, price_info, threshold=PROMO_MIN):
    """Only sale_price regular_amount > amount qualifies; no guessed discount."""
    if not isinstance(item, dict) or not isinstance(price_info, dict):
        return None
    if item.get("status") != "active":
        return None
    qty = item.get("available_quantity")
    if qty is not None and int(qty) <= 0:
        return None
    price = positive_amount(price_info.get("amount"))
    original = positive_amount(price_info.get("regular_amount"))
    currency = price_info.get("currency_id")
    if currency != "BRL" or price is None or original is None or price >= original:
        return None
    discount = round((original - price) * 100 / original, 2)
    if discount < threshold:
        return None
    return {"price": price, "original_price": original, "discount_percent": discount, "currency": currency}


def deactivate(item_id):
    with db() as conn:
        conn.execute(
            "UPDATE products SET is_promo=0, promo_checked_at=CURRENT_TIMESTAMP WHERE id=?",
            (item_id,),
        )


def verify_and_save(item_id, category, ranking=999):
    """Preserve the existing affiliate link, even when an item loses its discount."""
    item = ml_api("/items/" + item_id)
    sale = ml_api("/items/" + item_id + "/sale_price?context=channel_marketplace")
    promo = verified_promotion(item, sale)
    amount = positive_amount(sale.get("amount")) if isinstance(sale, dict) else None
    original = positive_amount(sale.get("regular_amount")) if isinstance(sale, dict) else None
    images = item.get("pictures") or []
    picture = item.get("thumbnail") or (images[0].get("secure_url", "") if images else "")
    with db() as conn:
        conn.execute(
            """INSERT INTO products
               (id,title,price,image,category,ranking,original_url,original_price,
                discount_percent,is_promo,promo_checked_at,currency,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, price=excluded.price,image=excluded.image,
                 category=excluded.category,ranking=excluded.ranking,
                 original_url=excluded.original_url,original_price=excluded.original_price,
                 discount_percent=excluded.discount_percent,is_promo=excluded.is_promo,
                 promo_checked_at=CURRENT_TIMESTAMP,currency=excluded.currency,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                item_id, item.get("title") or item_id, amount, picture,
                category, ranking, item.get("permalink", ""), original,
                promo["discount_percent"] if promo else 0,
                1 if promo else 0, sale.get("currency_id", "") if isinstance(sale, dict) else "",
            ),
        )
    return bool(promo)


def import_category(category):
    category = str(category).upper().strip()
    if not re.fullmatch(r"MLB\d+", category):
        raise ValueError("Categoria invalida. Informe um ID como MLB1051.")
    with IMPORT_LOCK:
        highlights = ml_api("/highlights/MLB/category/" + category)
        ranked = [
            obj for obj in highlights.get("content", [])
            if obj.get("type") == "ITEM" and VALID_ID.fullmatch(str(obj.get("id", "")))
        ]
        verified, discarded, failed = 0, 0, 0
        for entry in ranked[:20]:
            item_id = entry["id"]
            try:
                if verify_and_save(item_id, category, entry.get("position", 999)):
                    verified += 1
                else:
                    discarded += 1
            except (urllib.error.URLError, ValueError, KeyError, TypeError, RuntimeError):
                deactivate(item_id)
                failed += 1
        return {
            "category": category, "checked": len(ranked[:20]), "promotions": verified,
            "without_discount": discarded, "failed": failed,
            "catalog_types_not_supported": len(highlights.get("content", [])) - len(ranked),
        }


def refresh_linked_items():
    """Recheck published items; skip stale/invalid data instead of inventing prices."""
    with IMPORT_LOCK:
        with db() as conn:
            items = [
                dict(r) for r in conn.execute(
                    """SELECT id,category,ranking FROM products
                       WHERE affiliate_url IS NOT NULL AND affiliate_url <> ''
                       ORDER BY COALESCE(promo_checked_at,'') ASC LIMIT 100"""
                )
            ]
        verified, removed, failed = 0, 0, 0
        for row in items:
            try:
                if verify_and_save(row["id"], row["category"], row["ranking"]):
                    verified += 1
                else:
                    removed += 1
            except (urllib.error.URLError, ValueError, KeyError, TypeError, RuntimeError):
                deactivate(row["id"])
                failed += 1
        return {"checked": len(items), "promotions": verified, "removed": removed, "failed": failed}


def run_sync():
    global LAST_SYNC
    if not TOKEN:
        LAST_SYNC = {"time": None, "status": "sem token OAuth", "details": []}
        return
    details = []
    for category in CATEGORIES:
        try:
            details.append(import_category(category))
        except Exception as exc:
            details.append({"category": category, "error": type(exc).__name__})
    try:
        details.append({"refresh": refresh_linked_items()})
    except Exception as exc:
        details.append({"refresh_error": type(exc).__name__})
    LAST_SYNC = {
        "time": datetime.now(timezone.utc).isoformat(),
        "status": "concluido",
        "details": details,
    }


def background_job():
    while True:
        try:
            run_sync()
        except Exception as exc:
            LAST_SYNC.update(status="erro: " + type(exc).__name__)
        time.sleep(INTERVAL_HOURS * 3600)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def html(self, filename):
        path = BASE / filename
        if not path.is_file():
            return self.respond(503, {"error": "Arquivo de interface nao encontrado."})
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def is_admin(self):
        if not ADMIN_KEY:
            self.respond(503, {"error": "Configure OFERTIX_ADMIN_KEY no Render ou no terminal."})
            return False
        if not hmac.compare_digest(self.headers.get("X-Admin-Key", ""), ADMIN_KEY):
            self.respond(403, {"error": "Senha administrativa incorreta."})
            return False
        return True

    def read_body(self):
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 100000:
            raise ValueError("Corpo da solicitacao invalido.")
        return json.loads(self.rfile.read(size))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.html("index.html")
        if path in ("/admin", "/admin.html"):
            return self.html("admin.html")
        if path == "/api/health":
            return self.respond(200, {"ok": True, "service": "OFERTIX"})
        if path == "/api/products":
            with db() as conn:
                rows = [
                    dict(row) for row in conn.execute(
                        """SELECT id,title,price,original_price,discount_percent,image,category,
                                  affiliate_url,promo_checked_at
                           FROM products
                           WHERE is_promo=1 AND discount_percent >= ?
                             AND affiliate_url IS NOT NULL AND affiliate_url <> ''
                             AND promo_checked_at >= datetime('now', ?)
                           ORDER BY discount_percent DESC, COALESCE(ranking,999) ASC LIMIT 300""",
                        (PROMO_MIN, "-" + str(FRESH_HOURS) + " hours"),
                    )
                ]
            return self.respond(200, {"products": rows, "minimum_discount": PROMO_MIN})
        if path == "/api/admin/status":
            if not self.is_admin():
                return
            with db() as conn:
                total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                published = conn.execute(
                    """SELECT COUNT(*) FROM products WHERE is_promo=1 AND discount_percent>=?
                       AND promo_checked_at >= datetime('now',?)
                       AND affiliate_url IS NOT NULL AND affiliate_url<>''""",
                    (PROMO_MIN, "-" + str(FRESH_HOURS) + " hours"),
                ).fetchone()[0]
            return self.respond(200, {
                "token_configured": bool(TOKEN), "categories": CATEGORIES, "min_discount": PROMO_MIN,
                "refresh_every_hours": INTERVAL_HOURS, "max_age_hours": FRESH_HOURS,
                "total": total, "published": published, "last_sync": LAST_SYNC,
            })
        if path == "/api/admin/products":
            if not self.is_admin():
                return
            with db() as conn:
                items = [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM products ORDER BY discount_percent DESC,updated_at DESC LIMIT 500"
                    )
                ]
            return self.respond(200, {"products": items})
        return self.respond(404, {"error": "Rota nao encontrada."})

    def do_POST(self):
        if not self.is_admin():
            return
        try:
            path = urlparse(self.path).path
            body = self.read_body()
            if path == "/api/admin/link":
                item_id = str(body.get("id", "")).strip().upper()
                link = str(body.get("affiliate_url", "")).strip()
                if not VALID_ID.fullmatch(item_id) or not allowed_affiliate_link(link):
                    return self.respond(400, {"error": "Informe ID MLB valido e link HTTPS do Mercado Livre."})
                with db() as conn:
                    cur = conn.execute(
                        "UPDATE products SET affiliate_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (link, item_id),
                    )
                    if not cur.rowcount:
                        return self.respond(404, {"error": "Importe primeiro o produto pelo ID da categoria."})
                return self.respond(200, {"ok": True, "note": "Verifique no Gerador de Links do Mercado Livre se o URL informado realmente tem rastreio de afiliado."})
            if path == "/api/admin/import":
                if not TOKEN:
                    return self.respond(400, {"error": "Configure ML_ACCESS_TOKEN antes de importar produtos."})
                category = str(body.get("category", "")).strip().upper()
                return self.respond(200, import_category(category))
            if path == "/api/admin/refresh":
                if not TOKEN:
                    return self.respond(400, {"error": "ML_ACCESS_TOKEN nao configurado."})
                return self.respond(200, refresh_linked_items())
            return self.respond(404, {"error": "Rota administrativa inexistente."})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.respond(400, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            return self.respond(502, {"error": "Mercado Livre retornou HTTP " + str(exc.code) + ". Verifique token e permissao."})
        except Exception as exc:
            return self.respond(500, {"error": "Falha na operacao: " + type(exc).__name__})

    def log_message(self, format_string, *args):
        super().log_message(format_string, *args)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    print("OFERTIX: porta " + str(port), flush=True)
    if not ADMIN_KEY:
        print("AVISO: OFERTIX_ADMIN_KEY nao foi configurada.", flush=True)
    if not TOKEN:
        print("AVISO: ML_ACCESS_TOKEN ausente. Importacao aguarda OAuth.", flush=True)
    threading.Thread(target=background_job, name="ofertix-sync", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
