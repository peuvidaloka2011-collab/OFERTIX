"""Testes offline da automação Awin V17."""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import affiliate_feeds
import main
import public_affiliate_catalog as pac
from scripts.sync_awin_catalog import generate


def sample_row():
    return {
        "merchant_id": "321", "merchant_name": "Loja Parceira",
        "aw_product_id": "sku-1", "product_name": "Notebook para estudo",
        "currency": "BRL", "search_price": "900.00", "product_price_old": "1200.00",
        "aw_deep_link": "https://www.awin1.com/cread.php?awinaffid=456&awinmid=321",
        "merchant_image_url": "https://cdn.example.com/notebook.jpg",
        "merchant_category": "Informática", "in_stock": "1",
    }


def csv_text(*rows):
    import csv
    import io
    buf = io.StringIO()
    fields = list(sample_row())
    writer=csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def catalog(valid=True, *, discount=25):
    now=datetime.now(timezone.utc)
    start=(now-timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    finish=(now+timedelta(hours=22)).strftime("%Y-%m-%dT%H:%M:%SZ")
    price=900 if valid else 1200
    return {
        "version":1, "generated_at":start, "expires_at":finish,
        "products":[{
            "id":"abc", "merchant":"Loja Parceira", "title":"Notebook para estudo",
            "category":"Informática", "price":price, "original_price":1200,
            "discount_percent":discount,
            "affiliate_url":"https://www.awin1.com/cread.php?awinmid=321&awinaffid=456",
            "image":"https://cdn.example.com/notebook.png",
        }]
    }


class AwinAutomaticCatalogTests(unittest.TestCase):
    def test_authorized_feed_creates_public_catalog(self):
        with patch("scripts.sync_awin_catalog.fetch_awin_feed",
                   return_value=csv_text(sample_row())) as download:
            data, scanned = generate("https://productdata.awin.com/datafeed/download/apikey/SECRET")
        download.assert_called_once()
        self.assertEqual(scanned, 1)
        self.assertEqual(len(data["products"]), 1)
        self.assertEqual(data["products"][0]["discount_percent"], 25)
        self.assertEqual(data["products"][0]["merchant"],"Loja Parceira")
        self.assertIn("awinaffid=456",data["products"][0]["affiliate_url"])
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertEqual(len(pac.read_catalog(data)),1)

    def test_prevent_untracked_links_and_unproven_discounts(self):
        cases = [
            sample_row()|{"aw_deep_link":"https://merchant.example.com/product"},
            sample_row()|{"aw_deep_link":"http://www.awin1.com/cread.php"},
            sample_row()|{"search_price":"1200"},
            sample_row()|{"product_price_old":""},
            sample_row()|{"in_stock":"false"},
            sample_row()|{"currency":"USD"},
        ]
        for row in cases:
            with self.subTest(row=row):
                parsed=affiliate_feeds.parse_awin_csv(csv_text(row))
                self.assertEqual(parsed["offers"],[])

    def test_public_list_rejects_stale_and_fake_prices(self):
        sample=catalog()
        self.assertEqual(len(pac.read_catalog(sample)),1)
        sample["products"][0]["discount_percent"]=99
        self.assertEqual(pac.read_catalog(sample),[])
        sample["products"][0]["discount_percent"]=25
        sample["products"][0]["affiliate_url"]="https://bad.example.org/x"
        self.assertEqual(pac.read_catalog(sample),[])
        sample["products"][0]["affiliate_url"]="https://www.awin1.com/cread.php?x=1"
        sample["expires_at"]=(datetime.now(timezone.utc)-timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(pac.read_catalog(sample),[])

    def test_no_feed_secrets_leak_into_render(self):
        self.assertNotIn("apikey/",pac.CATALOG_URL)
        self.assertTrue(pac.CATALOG_URL.endswith("/ofertix-catalog/catalog_awin.json"))
        self.assertEqual(pac.read_catalog({"version":1,"products":[]}),[])

    def test_public_api_contains_authorized_affiliate_offers(self):
        with tempfile.TemporaryDirectory() as d:
            old_db=main.DB
            old_admin=main.ADMIN_KEY
            main.DB=Path(d)/"test.sqlite"
            main.ADMIN_KEY="test-key"
            main.init_db()
            with patch.object(pac,"get_offers",return_value=pac.read_catalog(catalog())):
                server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
                t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
                try:
                    conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=4)
                    conn.request("GET","/api/products")
                    res=conn.getresponse()
                    payload=json.loads(res.read())
                    self.assertEqual(res.status,200)
                    self.assertEqual(len(payload["products"]),1)
                    offer=payload["products"][0]
                    self.assertEqual(offer["merchant"],"Loja Parceira")
                    self.assertEqual(offer["pricing_source"],"affiliate_feed")
                    self.assertFalse(offer["verified_discount"])
                    conn.close()
                finally:
                    server.shutdown();server.server_close()
                    main.DB=old_db;main.ADMIN_KEY=old_admin

    def test_http_failure_returns_expired_cache_as_empty(self):
        pac._CACHE.update(at=0.0,offers=[],expires_at=None)
        with patch("public_affiliate_catalog.urllib.request.urlopen",
                   side_effect=OSError("Network unavailable")):
            self.assertEqual(pac.get_offers(),[])


if __name__=="__main__":
    unittest.main()
