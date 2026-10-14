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
from urllib.parse import urlparse, parse_qs, urljoin
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
IMPORT_STATUS_LOCK = threading.Lock()
IMPORT_STATUS = {"running": False, "category": None, "result": None, "error": None,
                 "started_at": None, "finished_at": None}
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


# Redirecionamentos aceitos SOMENTE dentro de dominios oficiais conhecidos.
# Nunca acessar servidores arbitrarios informados via Location (protecao SSRF).
AFFILIATE_HOSTS = frozenset({
    "meli.la", "www.meli.la", "mercadolivre.com", "www.mercadolivre.com",
    "mercadolivre.com.br", "www.mercadolivre.com.br",
    "produto.mercadolivre.com.br", "mercadolibre.com",
    "www.mercadolibre.com", "mercadolibre.com.br", "www.mercadolibre.com.br",
})


def allowed_affiliate_link(value):
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.netloc)
            and (parsed.hostname or "").lower() in AFFILIATE_HOSTS
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except (TypeError, ValueError):
        return False


def listing_id_from_url(url):
    """Extrai apenas IDs MLB de caminho de anuncio; /p/MLB e produto de catalogo."""
    if not allowed_affiliate_link(url):
        return None
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() in {"meli.la", "www.meli.la"}:
        return None
    path = parsed.path
    if re.search(r"(?:^|/)p/MLB\d+(?:[/_-]|$)", path, re.I):
        return None
    # Nao usar querystring: parametros de rastreio podem trazer outros IDs.
    match = re.search(r"(?:^|[/_])(MLB)-?(\d{6,15})(?=$|[-/_])", path, re.I)
    return "MLB" + match.group(2) if match else None


class _NoAffiliateRedirects(urllib.request.HTTPRedirectHandler):
    """A aplicacao verifica o proximo destino ANTES de segui-lo."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def affiliate_redirect_destination(url):
    """Consulta apenas o header Location. Nao baixa nem processa HTML de terceiros."""
    opener = urllib.request.build_opener(_NoAffiliateRedirects())
    req = urllib.request.Request(
        url, headers={"User-Agent": "OFERTIX/9.0", "Accept": "text/html"}, method="GET"
    )
    try:
        with opener.open(req, timeout=5) as response:
            return response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location") if exc.headers else None
        raise ValueError(
            "O link nao pode ser aberto automaticamente (HTTP "
            + str(exc.code) + "). Informe o ID do anuncio manualmente."
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError(
            "Nao foi possivel abrir o link de afiliado. Informe o ID do anuncio manualmente."
        ) from None


def find_affiliate_listing_id(link):
    """Segue no maximo 5 redirecionamentos oficiais, sem consultar busca bloqueada.

    Links de catalogo ou sem ID verificavel exigem preenchimento manual.
    Nao presume que codigo MLB de produto de catalogo e anuncio do vendedor.
    """
    if len(link) > 2048 or not allowed_affiliate_link(link):
        raise ValueError("Cole um link HTTPS oficial do Mercado Livre ou meli.la.")
    visited = set()
    current = link
    for _ in range(6):
        if not allowed_affiliate_link(current):
            raise ValueError(
                "O link direciona para outro dominio. Informe manualmente o ID do anuncio."
            )
        if current in visited:
            raise ValueError("O link entrou em redirecionamento repetido. Informe o ID manualmente.")
        visited.add(current)
        item_id = listing_id_from_url(current)
        if item_id:
            return item_id
        if re.search(r"(?:^|/)p/MLB\d+(?:[/_-]|$)", urlparse(current).path, re.I):
            raise ValueError(
                "O link abriu uma pagina de catalogo, sem ID de anuncio individual. "
                "Informe o ID do anuncio manualmente em Opcoes avancadas."
            )
        if len(visited) > 5:
            break
        location = affiliate_redirect_destination(current)
        if not location:
            break
        current = urljoin(current, location)
    raise ValueError(
        "Nao foi possivel identificar o ID do anuncio nesse link. "
        "Abra Opcoes avancadas para informar o ID MLB manualmente."
    )


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


def register_affiliate_item(item_id, link):
    """Registra link oficial para anuncio MLB, mesmo sem acesso a busca por categoria.

    Nunca inventa preco ou desconto: se API nao responder, fica pendente e oculto.
    A validade/rastreio do link de afiliado e responsabilidade do gerador oficial.
    """
    item_id = str(item_id).strip().upper()
    link = str(link).strip()
    if len(link) > 2048 or not allowed_affiliate_link(link):
        raise ValueError("Cole um link HTTPS gerado no programa de afiliados do Mercado Livre.")
    detection = "manual" if item_id else "automatico"
    if not item_id:
        item_id = find_affiliate_listing_id(link)
    if not VALID_ID.fullmatch(item_id):
        raise ValueError("Informe um ID de anuncio MLB valido, nao um codigo de catalogo.")
    # Se o destino ja expõe um anuncio, nunca vincular a outro ID digitado por engano.
    direct_item = listing_id_from_url(link)
    if direct_item and direct_item != item_id:
        raise ValueError("O ID digitado nao corresponde ao anuncio indicado no link.")
    verified = False
    promo = False
    reason = None

    # Evita colisao com importacao, revalidacao e salvamento simultaneo do mesmo item.
    with IMPORT_LOCK:
        if TOKEN or oauth_ml.connected(DB):
            try:
                promo = verify_and_save(item_id, category=None)
                verified = True
            except urllib.error.HTTPError as exc:
                reason = "Consulta do anuncio recusada pela API (HTTP " + str(exc.code) + ")."
            except (urllib.error.URLError, TimeoutError):
                reason = "Nao foi possivel consultar o Mercado Livre neste momento."
            except (oauth_ml.OAuthFlowError, oauth_ml.OAuthConfigError, RuntimeError):
                reason = "Autorizacao da API indisponivel. Reconecte o Mercado Livre pelo painel."
            except (ValueError, KeyError, TypeError):
                reason = "A API retornou dados insuficientes para verificar o desconto."
        else:
            reason = "A conta Mercado Livre ainda nao esta conectada para consultar precos."

        # Nao publicar desconto antigo se a tentativa de revalidacao falhar.
        with db() as conn:
            conn.execute(
                """INSERT INTO products (id,title,affiliate_url,is_promo,discount_percent,updated_at)
                   VALUES (?,?,?,0,0,CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET
                     affiliate_url=excluded.affiliate_url,
                     is_promo=CASE WHEN ? THEN products.is_promo ELSE 0 END,
                     promo_checked_at=CASE WHEN ? THEN products.promo_checked_at ELSE NULL END,
                     updated_at=CURRENT_TIMESTAMP""",
                (item_id, "Aguardando verificacao: " + item_id, link,
                 1 if verified else 0, 1 if verified else 0),
            )

    return {
        "ok": True,
        "id": item_id,
        "detection": detection,
        "checked": verified,
        "promotion_confirmed": bool(verified and promo),
        "publicly_visible": bool(verified and promo),
        "status": (
            "Oferta verificada, elegivel para aparecer na vitrine."
            if verified and promo else
            "Produto consultado, mas nao foi confirmado desconto minimo de "
            + str(PROMO_MIN) + "%."
            if verified else
            "Link cadastrado como pendente, sem publicacao. " + (reason or "")
        ),
        "note": "O OFERTIX nao comprova atribuicao de comissao; use link gerado na Central/Barra de Afiliados.",
    }


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


def resolve_user_product(up_id):
    """USER_PRODUCT normalmente representa anúncios do vendedor; nunca criar item fictício."""
    if not re.fullmatch(r"MLBU\d+", up_id):
        return []
    user_product = ml_api("/user-products/" + up_id)
    if not isinstance(user_product, dict):
        return []
    user_id = str(user_product.get("user_id") or "")
    if not user_id.isdecimal() or not 0 < int(user_id) < 10**18:
        return []
    search = ml_api("/users/" + user_id + "/items/search?user_product_id=" + up_id)
    if not isinstance(search, dict) or not isinstance(search.get("results"), list):
        return []
    found = []
    for entry in search["results"]:
        item_id = str(entry).upper()
        if VALID_ID.fullmatch(item_id) and item_id not in found:
            found.append(item_id)
        if len(found) >= 2:
            break
    return found


def resolve_catalog_product(product_id, depth=0, visited=None):
    """Segue uma pequena amostra de variantes quando um pai nao possui buy box.

    A API permite produtos-pai inativos com filhos compraveis. Limitamos o numero
    de chamadas para nao percorrer arbitrariamente a arvore inteira.
    """
    if not VALID_ID.fullmatch(product_id):
        return []
    if visited is None:
        visited = set()
    if product_id in visited or len(visited) >= 7 or depth > 2:
        return []
    visited.add(product_id)
    product = ml_api("/products/" + product_id)
    if not isinstance(product, dict):
        return []
    if product.get("status") == "active":
        winner = product.get("buy_box_winner")
        item_id = str(winner.get("item_id", "")).upper() if isinstance(winner, dict) else ""
        if VALID_ID.fullmatch(item_id):
            return [item_id]
    # Nao deduzir preco do parent; percorrer somente folhas com anuncio real.
    children = [
        str(item).upper() for item in (product.get("children_ids") or [])
        if VALID_ID.fullmatch(str(item).upper())
    ]
    if not children:
        for picker in (product.get("pickers") or []):
            if not isinstance(picker, dict):
                continue
            for option in picker.get("products") or []:
                if not isinstance(option, dict):
                    continue
                child = str(option.get("product_id", "")).upper()
                if VALID_ID.fullmatch(child) and child != product_id:
                    children.append(child)
    # Limitar: no maximo 4 filhos por nivel, 7 consultas totais.
    found = []
    for child in dict.fromkeys(children[:4]):
        if len(visited) >= 7:
            break
        try:
            for item_id in resolve_catalog_product(child, depth + 1, visited):
                if item_id not in found:
                    found.append(item_id)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 404):
                raise
        if len(found) >= 2:
            break
    return found[:2]


def ranked_item_candidates(entry):
    """Traduz ranking de ITEM, PRODUCT e USER_PRODUCT em anúncios MLB reais.

    O endpoint /highlights documenta inclusive ITEM com identificador MLBU.
    """
    kind = str(entry.get("type", "")).upper()
    ranked_id = str(entry.get("id", "")).upper()
    if kind == "ITEM":
        if VALID_ID.fullmatch(ranked_id):
            return [ranked_id]
        if re.fullmatch(r"MLBU\d+", ranked_id):
            return resolve_user_product(ranked_id)
        return []
    if kind == "PRODUCT":
        return resolve_catalog_product(ranked_id)
    if kind == "USER_PRODUCT":
        return resolve_user_product(ranked_id)
    return []


def diagnose_catalog(category, limit=3):
    """Leitura de amostra do ranking: apenas informacoes estruturais e nao sensiveis.

    Nao publica produtos e nao expoe URL de autorizacao, tokens ou dados pessoais.
    """
    details = category_details(category)
    if not details["is_final"]:
        return {"category": category, "is_final": False,
                "message": "Escolha uma categoria final para diagnosticar."}
    ranking = ml_api("/highlights/MLB/category/" + category)
    if not isinstance(ranking, dict) or not isinstance(ranking.get("content"), list):
        raise ValueError("Ranking nao retornou dados validos.")
    sample = []
    for entry in ranking["content"]:
        if len(sample) >= limit:
            break
        if not isinstance(entry, dict) or entry.get("type") != "PRODUCT":
            continue
        product_id = str(entry.get("id", "")).upper()
        if not VALID_ID.fullmatch(product_id):
            continue
        try:
            obj = ml_api("/products/" + product_id)
            if not isinstance(obj, dict):
                sample.append({"product_id": product_id, "error": "Formato nao reconhecido."})
                continue
            winner = obj.get("buy_box_winner")
            winner_id = str(winner.get("item_id", "")).upper() if isinstance(winner, dict) else ""
            children = obj.get("children_ids") or []
            pickers = obj.get("pickers") or []
            pickers_count = sum(
                len(p.get("products") or []) for p in pickers if isinstance(p, dict)
            )
            sample.append({
                "product_id": product_id,
                "status": obj.get("status", "desconhecido"),
                "buy_box_winner_has_item_id": bool(VALID_ID.fullmatch(winner_id)),
                "has_parent_id": bool(obj.get("parent_id")),
                "children_count": len(children) if isinstance(children, list) else 0,
                "picker_options_count": pickers_count,
                "type": obj.get("type", "nao informado"),
            })
        except urllib.error.HTTPError as exc:
            sample.append({"product_id": product_id, "error": "HTTP " + str(exc.code)})
        except (urllib.error.URLError, TypeError, ValueError, KeyError):
            sample.append({"product_id": product_id, "error": "Consulta nao concluida."})
    return {
        "category": category,
        "ranking_count": len(ranking["content"]),
        "sample_size": len(sample),
        "sample": sample,
        "note": "Diagnostico somente leitura. IDs sao publicos; nao compartilhe credenciais.",
    }


def import_category(category):
    category = str(category).upper().strip()
    details = category_details(category)
    if not details["is_final"]:
        return {
            **details, "checked": 0, "promotions": 0,
            "requires_subcategory": True,
            "message": "Essa categoria tem subdivisoes. Escolha uma subcategoria final para importar.",
        }
    with IMPORT_LOCK:
        try:
            highlights = ml_api("/highlights/MLB/category/" + category)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {
                    "category": category, "checked": 0, "promotions": 0,
                    "ranking_unavailable": True,
                    "message": "Essa categoria final nao possui ranking de mais vendidos disponivel.",
                }
            raise

        content = highlights.get("content", []) if isinstance(highlights, dict) else None
        if not isinstance(content, list):
            raise ValueError("O ranking retornou um formato inesperado.")

        ranking = [
            entry for entry in content[:20]
            if isinstance(entry, dict) and entry.get("type") in ("ITEM", "PRODUCT", "USER_PRODUCT")
        ]
        type_counts = {
            kind: sum(1 for entry in ranking if entry.get("type") == kind)
            for kind in ("ITEM", "PRODUCT", "USER_PRODUCT")
        }
        verified = discarded = failed = checked = catalog_unresolved = catalog_errors = 0
        resolved_catalog = 0
        repeated = 0
        seen_item_ids = set()
        reasons = []
        for entry in ranking:
            kind = entry["type"]
            try:
                candidates = ranked_item_candidates(entry)
            except urllib.error.HTTPError as error:
                catalog_errors += 1
                if len(reasons) < 3:
                    reasons.append(kind + " indisponivel (HTTP " + str(error.code) + ")")
                continue
            except (urllib.error.URLError, KeyError, ValueError, TypeError, RuntimeError):
                catalog_errors += 1
                if len(reasons) < 3:
                    reasons.append("Falha ao resolver identificador " + kind)
                continue

            if not candidates:
                # Inclui ITEM com identificador MLBU documentado no ranking.
                catalog_unresolved += 1
                continue
            if kind != "ITEM":
                resolved_catalog += 1
            for item_id in candidates:
                if item_id in seen_item_ids:
                    repeated += 1
                    continue
                seen_item_ids.add(item_id)
                checked += 1
                try:
                    if verify_and_save(item_id, category, entry.get("position", 999)):
                        verified += 1
                    else:
                        discarded += 1
                except urllib.error.HTTPError as error:
                    deactivate(item_id)
                    failed += 1
                    if len(reasons) < 3:
                        reasons.append("Consulta de preco ou anuncio: HTTP " + str(error.code))
                except (urllib.error.URLError, ValueError, KeyError, TypeError, RuntimeError):
                    deactivate(item_id)
                    failed += 1

        # Fallback documentado: buscar anúncios por categoria quando os produtos
        # mais vendidos nao fornecem um item MLB diretamente.
        # Os resultados deste endpoint NAO sao necessariamente os mais vendidos.
        fallback_attempted = checked == 0
        fallback_returned = 0
        fallback_checked = 0
        fallback_error = None
        if fallback_attempted:
            try:
                search = ml_api("/sites/MLB/search?category=" + category + "&limit=20")
                listing_rows = search.get("results") if isinstance(search, dict) else None
                if not isinstance(listing_rows, list):
                    raise ValueError("Busca de anuncios retornou formato inesperado.")
                listing_ids = []
                for row in listing_rows[:20]:
                    item_id = str(row.get("id", "")).upper() if isinstance(row, dict) else ""
                    if VALID_ID.fullmatch(item_id) and item_id not in listing_ids:
                        listing_ids.append(item_id)
                fallback_returned = len(listing_ids)
                for item_id in listing_ids:
                    if item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(item_id)
                    checked += 1
                    fallback_checked += 1
                    try:
                        if verify_and_save(item_id, category):
                            verified += 1
                        else:
                            discarded += 1
                    except urllib.error.HTTPError as error:
                        deactivate(item_id)
                        failed += 1
                        if len(reasons) < 3:
                            reasons.append("Preco de anuncio por categoria: HTTP " + str(error.code))
                    except (urllib.error.URLError, ValueError, KeyError, TypeError, RuntimeError):
                        deactivate(item_id)
                        failed += 1
            except urllib.error.HTTPError as error:
                fallback_error = "Busca por categoria: HTTP " + str(error.code)
            except (urllib.error.URLError, ValueError, KeyError, TypeError):
                fallback_error = "Busca por categoria indisponivel ou com formato inesperado."

        if fallback_checked:
            message = (
                "Os produtos do ranking nao forneceram anuncios MLB. A busca oficial "
                "por categoria retornou anuncios (NAO necessariamente mais vendidos) "
                "que foram verificados por preco real. Publicacao requer link oficial de afiliado."
            )
        elif fallback_attempted:
            message = (
                "O ranking nao forneceu anuncios verificaveis. "
                + (fallback_error or "A busca por categoria tambem nao retornou anuncios MLB acessiveis.")
                + " Nenhuma promocao foi inventada."
            )
        elif not ranking:
            message = "O ranking nao retornou tipos ITEM, PRODUCT ou USER_PRODUCT."
        elif not checked:
            message = (
                "O ranking retornou produtos, mas nenhum anuncio com ID MLB acessivel "
                "foi identificado. Verifique as permissoes da API ou tente outra categoria."
            )
        else:
            message = (
                "Anuncios consultados. Apenas descontos comprovados de pelo menos "
                + str(PROMO_MIN) + "% entram no catalogo; links de afiliado oficiais "
                "ainda precisam ser associados para publicacao."
            )
        return {
            "category": category,
            "ranking_entries": len(ranking),
            "checked": checked,
            "promotions": verified,
            "without_discount": discarded,
            "failed": failed,
            "resolved_catalog": resolved_catalog,
            "catalog_unresolved": catalog_unresolved,
            "catalog_errors": catalog_errors,
            "unresolved_entries": catalog_unresolved,
            "item_mlbu_entries": sum(
                1 for entry in ranking
                if entry.get("type") == "ITEM" and str(entry.get("id", "")).startswith("MLBU")
            ),
            # Legacy field name retained to avoid breaking older dashboard clients.
            "catalog_types_not_supported": catalog_unresolved + catalog_errors,
            "duplicate_items_skipped": repeated,
            "ranking_types": type_counts,
            "category_search_attempted": fallback_attempted,
            "category_search_returned": fallback_returned,
            "category_search_checked": fallback_checked,
            "category_search_error": fallback_error,
            "diagnostics": reasons,
            "message": message,
        }


def import_in_background(category):
    """Executa consulta demorada fora da requisicao HTTP. Nunca expoe OAuth."""
    global IMPORT_STATUS
    try:
        result = import_category(category)
        with IMPORT_STATUS_LOCK:
            IMPORT_STATUS = {
                **IMPORT_STATUS, "running": False, "result": result, "error": None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
    except urllib.error.HTTPError as exc:
        with IMPORT_STATUS_LOCK:
            IMPORT_STATUS = {
                **IMPORT_STATUS, "running": False, "result": None,
                "error": "Mercado Livre respondeu HTTP " + str(exc.code),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as exc:
        with IMPORT_STATUS_LOCK:
            IMPORT_STATUS = {
                **IMPORT_STATUS, "running": False, "result": None,
                "error": "Falha na importacao: " + type(exc).__name__,
                "finished_at": datetime.now(timezone.utc).isoformat(),
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
        if path == "/api/admin/catalog/diagnose":
            if not self.is_admin():
                return
            query = parse_qs(urlparse(self.path).query)
            category = query.get("category", [""])[0].strip().upper()
            if not re.fullmatch(r"MLB\d+", category):
                return self.respond(400, {"error": "Categoria MLB invalida."})
            try:
                return self.respond(200, diagnose_catalog(category))
            except ValueError as exc:
                return self.respond(400, {"error": str(exc)})
            except urllib.error.HTTPError as exc:
                return self.respond(502, {"error": "Mercado Livre respondeu HTTP " + str(exc.code)})
            except Exception as exc:
                return self.respond(502, {"error": "Diagnostico indisponivel: " + type(exc).__name__})
        if path == "/api/admin/import/status":
            if not self.is_admin():
                return
            with IMPORT_STATUS_LOCK:
                return self.respond(200, dict(IMPORT_STATUS))
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
                result = register_affiliate_item(body.get("id", ""), body.get("affiliate_url", ""))
                return self.respond(200, result)
            if path == "/api/admin/import":
                if not TOKEN and not oauth_ml.connected(DB):
                    return self.respond(400, {"error": "Conecte sua conta Mercado Livre pelo painel."})
                category = str(body.get("category", "")).strip().upper()
                if not re.fullmatch(r"MLB\d+", category):
                    return self.respond(400, {"error": "Informe um ID de categoria MLB valido."})
                global IMPORT_STATUS
                with IMPORT_STATUS_LOCK:
                    if IMPORT_STATUS["running"]:
                        return self.respond(409, {"error": "Ja existe importacao em andamento."})
                    IMPORT_STATUS = {
                        "running": True, "category": category, "result": None, "error": None,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": None,
                    }
                threading.Thread(target=import_in_background, args=(category,),
                                 daemon=True, name="ofertix-import-manual").start()
                return self.respond(202, {"started": True, "category": category})
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
