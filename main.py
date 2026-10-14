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
from urllib.parse import urlparse, parse_qs
import html
import oauth_ml

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
    oauth_ml.init(DB)


def current_token():
    if oauth_ml.connected(DB):
        return oauth_ml.access_token(DB)
    return TOKEN


def ml_api(path):
    token = current_token()
    if not token:
        raise RuntimeError("Autorize o Mercado Livre no painel ou configure token temporario.")
    req = urllib.request.Request(
        "https://api.mercadolibre.com" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "OFERTIX/2.0"},
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


def category_details(category):
    """Consulta a arvore oficial; rankings so aceitam categorias finais."""
    category = str(category).strip().upper()
    if not re.fullmatch(r"MLB\d+", category):
        raise ValueError("Codigo de categoria invalido. Use um ID MLB numerico.")
    info = ml_api("/categories/" + category)
    if not isinstance(info, dict) or "children_categories" not in info:
        raise ValueError("A API nao retornou dados suficientes da categoria.")
    children = [
        {"id": x["id"], "name": x.get("name", x["id"])}
        for x in (info.get("children_categories") or [])
        if isinstance(x, dict) and re.fullmatch(r"MLB\d+", str(x.get("id", "")))
    ]
    return {
        "id": category,
        "name": info.get("name", category),
        "is_final": not bool(children),
        "subcategories": children[:35],
        "subcategories_total": len(children),
    }


def import_category(category):
    category = str(category).upper().strip()
    details = category_details(category)
    if not details["is_final"]:
        return {
            **details, "checked": 0, "promotions": 0,
            "requires_subcategory": True,
            "message": "Essa categoria tem subdivisoes. Escolha uma subcategoria ate chegar a uma categoria final, e tente importar novamente.",
        }
    with IMPORT_LOCK:
        try:
            highlights = ml_api("/highlights/MLB/category/" + category)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {
                    "category": category, "checked": 0, "promotions": 0,
                    "ranking_unavailable": True,
                    "message": "Essa categoria final nao possui um ranking de mais vendidos disponivel no Mercado Livre.",
                }
            raise
        content = highlights.get("content", [])
        if not isinstance(content, list):
            raise ValueError("O ranking retornou um formato inesperado.")
        ranked = [
            obj for obj in content
            if isinstance(obj, dict)
            and obj.get("type") == "ITEM"
            and VALID_ID.fullmatch(str(obj.get("id", "")))
        ]
        type_counts = {
            kind: sum(1 for obj in content if isinstance(obj, dict) and obj.get("type") == kind)
            for kind in ("ITEM", "PRODUCT", "USER_PRODUCT")
        }
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
            "catalog_types_not_supported": len(content) - len(ranked),
            "ranking_types": type_counts,
            "message": (
                "O ranking retornou apenas produtos de catalogo (PRODUCT/USER_PRODUCT). "
                "Esses IDs ainda precisam de resolucao para anuncios antes da consulta de precos."
                if not ranked and content else
                "A verificacao terminou. Promocoes validas so aparecem ao publico depois que "
                "voce associar seus links oficiais de afiliado."
            ),
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
    if not TOKEN and not oauth_ml.connected(DB):
        LAST_SYNC = {"time": None, "status": "sem autorizacao OAuth", "details": []}
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

    def oauth_page(self, status, title, message):
        # Nunca devolva o code, state ou tokens ao navegador.
        page = ('<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>OFERTIX - autorizacao</title>'
                '<body style="font:16px system-ui;background:#f3f8f4;color:#16452e;padding:30px">'
                '<h2>' + html.escape(title) + '</h2><p>' + html.escape(message) + '</p>'
                '<a href="/admin">Abrir painel OFERTIX</a></body></html>')
        payload = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
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
        if path == "/oauth/callback":
            params = parse_qs(urlparse(self.path).query)
            if params.get("error"):
                return self.oauth_page(400, "Autorizacao negada", "O Mercado Livre nao autorizou o acesso.")
            try:
                code = params.get("code", [""])[0]
                state = params.get("state", [""])[0]
                oauth_ml.exchange(DB, state, code)
            except (oauth_ml.OAuthFlowError, oauth_ml.OAuthConfigError) as exc:
                return self.oauth_page(400, "Nao foi possivel conectar", str(exc))
            except Exception:
                return self.oauth_page(502, "Erro na conexao", "Tente novamente pelo painel.")
            if CATEGORIES:
                threading.Thread(target=run_sync, name="ofertix-after-oauth", daemon=True).start()
            return self.oauth_page(200, "Mercado Livre conectado!",
                                   "Volte para a aba do painel OFERTIX e toque em Atualizar painel.")
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
                "token_configured": bool(TOKEN or oauth_ml.connected(DB)),
                "oauth_ready": oauth_ml.ready(), "oauth_connected": oauth_ml.connected(DB),
                "categories": CATEGORIES, "min_discount": PROMO_MIN,
                "refresh_every_hours": INTERVAL_HOURS, "max_age_hours": FRESH_HOURS,
                "total": total, "published": published, "last_sync": LAST_SYNC,
            })
        if path == "/api/admin/category":
            if not self.is_admin():
                return
            query = parse_qs(urlparse(self.path).query)
            category = query.get("id", [""])[0].strip().upper()
            try:
                return self.respond(200, category_details(category))
            except ValueError as exc:
                return self.respond(400, {"error": str(exc)})
            except urllib.error.HTTPError as exc:
                return self.respond(502, {"error": "A API Mercado Livre retornou HTTP " + str(exc.code) + " ao consultar categorias."})
            except Exception:
                return self.respond(502, {"error": "Falha ao consultar categorias do Mercado Livre."})
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
            if path == "/api/admin/oauth/start":
                return self.respond(200, {"authorization_url": oauth_ml.start(DB)})
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
                if not TOKEN and not oauth_ml.connected(DB):
                    return self.respond(400, {"error": "Conecte sua conta Mercado Livre pelo painel."})
                category = str(body.get("category", "")).strip().upper()
                return self.respond(200, import_category(category))
            if path == "/api/admin/refresh":
                if not TOKEN and not oauth_ml.connected(DB):
                    return self.respond(400, {"error": "Conecte sua conta Mercado Livre pelo painel."})
                return self.respond(200, refresh_linked_items())
            return self.respond(404, {"error": "Rota administrativa inexistente."})
        except oauth_ml.OAuthConfigError as exc:
            return self.respond(400, {"error": str(exc)})
        except oauth_ml.OAuthFlowError as exc:
            return self.respond(502, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.respond(400, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            return self.respond(502, {"error": "Mercado Livre retornou HTTP " + str(exc.code) + ". Confira codigo de categoria, permissao e disponibilidade do ranking."})
        except Exception as exc:
            return self.respond(500, {"error": "Falha na operacao: " + type(exc).__name__})

    def log_message(self, format_string, *args):
        # A URL de retorno contem um codigo OAuth e nao pode entrar nos logs.
        print("HTTP", self.command, urlparse(self.path).path, flush=True)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    print("OFERTIX: porta " + str(port), flush=True)
    if not ADMIN_KEY:
        print("AVISO: OFERTIX_ADMIN_KEY nao foi configurada.", flush=True)
    if not TOKEN and not oauth_ml.connected(DB):
        print("AVISO: Mercado Livre aguarda autorizacao OAuth pelo painel.", flush=True)
    threading.Thread(target=background_job, name="ofertix-sync", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
