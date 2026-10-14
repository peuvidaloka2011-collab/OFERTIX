"""Importa um ou mais feeds Awin autorizados para o catálogo público OFERTIX.

Uso no GitHub Actions: OFERTIX_AWIN_FEED_URLS (segredo, URLs por linha)
e --output (arquivo temporario). Nunca loga as URLs ou chaves.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Suporte a execucao via python scripts/sync_awin_catalog.py no checkout GitHub.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from affiliate_feeds import fetch_awin_feed, parse_awin_csv


def generate(urls, *, at=None, minimum_discount=10):
    links = [x.strip() for x in urls.splitlines() if x.strip()]
    if not links or len(links) > 8:
        raise ValueError("Configure de 1 a 8 URLs oficiais de feeds Awin.")
    at = at or datetime.now(timezone.utc)
    offers = {}
    scanned = 0
    for url in links:
        feed = fetch_awin_feed(url)
        parsed = parse_awin_csv(feed, minimum_discount=minimum_discount)
        scanned += parsed["scanned"]
        for offer in parsed["offers"]:
            offers[offer["id"]] = offer
        if len(offers) > 4000:
            raise ValueError("Muitas ofertas. Reduza categorias/lojas nos feeds.")
    selected = sorted(offers.values(), key=lambda row:row["discount_percent"], reverse=True)[:300]
    return {
        "version": 1,
        "generated_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (at + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "products": selected,
    }, scanned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    links = os.getenv("OFERTIX_AWIN_FEED_URLS", "")
    if not links.strip():
        print("OFERTIX: feed nao configurado; sem alteracoes no catalogo.")
        return 0
    try:
        catalog, scanned = generate(links)
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(catalog, ensure_ascii=False, separators=(",",":"))+"\n",
            encoding="utf-8",
        )
        print(f"OFERTIX: {scanned} entradas analisadas, "
              f"{len(catalog['products'])} ofertas elegiveis geradas.")
        return 0
    except Exception:
        print("OFERTIX: importacao nao concluida. "
              "O catalogo publico anterior permanece ate expirar.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
