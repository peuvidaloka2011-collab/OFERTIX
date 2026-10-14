"""Testes offline da integracao Awin. Nao utiliza tokens reais nem rede."""
import csv
import gzip
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import affiliate_feeds as af
import main


HEADERS = [
    "merchant_id", "merchant_name", "aw_product_id", "product_name", "currency",
    "search_price", "product_price_old", "aw_deep_link", "merchant_image_url",
    "merchant_category", "in_stock"
]


def sample_csv(*rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def product(**updates):
    result = {
        "merchant_id": "321", "merchant_name": "Loja Exemplo",
        "aw_product_id": "p-123", "product_name": "Fone sem fio Bluetooth",
        "currency": "BRL", "search_price": "89.90", "product_price_old": "129.90",
        "aw_deep_link": "https://www.awin1.com/cread.php?awinmid=321&awinaffid=456&p=test",
        "merchant_image_url": "https://cdn.example.com/fone.jpg",
        "merchant_category": "Eletronicos", "in_stock": "1"
    }
    result.update(updates)
    return result


class AffiliateFeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = main.DB
        self.old_admin = main.ADMIN_KEY
        self.old_feed = main.AWIN_FEED_URL
        self.old_status = dict(af.LAST_SYNC)
        main.DB = Path(self.tmp.name) / "ofertix.sqlite"
        main.ADMIN_KEY = "test-feed-secret"
        main.AWIN_FEED_URL = ""
        main.init_db()

    def tearDown(self):
        main.DB = self.old_db
        main.ADMIN_KEY = self.old_admin
        main.AWIN_FEED_URL = self.old_feed
        af.LAST_SYNC.clear()
        af.LAST_SYNC.update(self.old_status)
        self.tmp.cleanup()

    def test_feed_imports_prices_photo_and_real_affiliate_link(self):
        parsed = af.parse_awin_csv(sample_csv(product()), minimum_discount=10)
        self.assertEqual(parsed["scanned"], 1)
        self.assertEqual(len(parsed["offers"]), 1)
        offer = parsed["offers"][0]
        self.assertEqual(offer["merchant"], "Loja Exemplo")
        self.assertEqual(offer["price"], 89.90)
        self.assertEqual(offer["original_price"], 129.90)
        self.assertGreater(offer["discount_percent"], 30)
        self.assertIn("awinaffid=456", offer["affiliate_url"])
        with main.db() as conn:
            af.import_parsed(conn, parsed)
            live = af.visible_offers(conn)
        self.assertEqual(live[0]["network"], "Awin")
        self.assertEqual(live[0]["pricing_source"], "affiliate_feed")
        self.assertFalse(live[0]["verified_discount"])
        self.assertEqual(live[0]["merchant"], "Loja Exemplo")

    def test_skips_missing_old_price_rrp_only_and_empty_discount(self):
        rows = [
            product(product_price_old=""),
            product(search_price="129.90",product_price_old="129.90"),
            product(search_price="120.0",product_price_old="129.90"),
        ]
        parsed=af.parse_awin_csv(sample_csv(*rows))
        self.assertEqual(parsed["scanned"],3)
        self.assertEqual(parsed["offers"],[])

    def test_skips_untracked_shop_links_out_of_stock_and_wrong_currency(self):
        rows = [
            product(aw_deep_link="https://www.mercadolivre.com.br/produto",aw_product_id="2"),
            product(aw_deep_link="javascript:alert(1)",aw_product_id="3"),
            product(aw_deep_link="https://awin1.com.evil.org/cread.php",aw_product_id="4"),
            product(in_stock="0",aw_product_id="5"),
            product(currency="USD",aw_product_id="6"),
            product(merchant_image_url="http://images.example.com/prod.png",aw_product_id="7"),
        ]
        parsed=af.parse_awin_csv(sample_csv(*rows))
        self.assertEqual(parsed["offers"],[])

    def test_locale_and_unique_multi_merchant(self):
        p1=product(search_price="1.234,50",product_price_old="1.799,90",
                   aw_product_id="SKU7",merchant_id="12",merchant_name="Loja A")
        p2=product(search_price="1,20",product_price_old="2,00",
                   aw_product_id="SKU7",merchant_id="13",merchant_name="Loja B")
        parsed=af.parse_awin_csv(sample_csv(p1,p2))
        self.assertEqual(len(parsed["offers"]),2)
        self.assertEqual(parsed["offers"][0]["price"],1234.5)
        self.assertEqual(parsed["offers"][1]["price"],1.2)
        with main.db() as conn:
            self.assertEqual(af.import_parsed(conn,parsed),2)
            self.assertEqual(af.import_parsed(conn,parsed),2)
            count=conn.execute("SELECT COUNT(*) FROM affiliate_feed_offers").fetchone()[0]
        self.assertEqual(count,2)

    def test_time_based_expiry_hides_offers_without_running_worker(self):
        parsed=af.parse_awin_csv(sample_csv(product()))
        with main.db() as conn:
            af.import_parsed(conn,parsed,
                at=datetime.now(timezone.utc)-timedelta(hours=26),ttl_hours=24)
            self.assertEqual(af.visible_offers(conn),[])
            af.import_parsed(conn,parsed)
            self.assertEqual(len(af.visible_offers(conn)),1)

    def test_wrong_columns_and_oversized_feeds_rejected_without_deleting_existing(self):
        parsed=af.parse_awin_csv(sample_csv(product()))
        with main.db() as conn:af.import_parsed(conn,parsed)
        with self.assertRaises(ValueError):
            af.parse_awin_csv("sku,price\n123,20\n")
        with self.assertRaises(ValueError):
            af.parse_awin_csv("x"* (af.MAX_CSV_BYTES+1))
        with main.db() as conn:
            self.assertEqual(len(af.visible_offers(conn)),1)

    def test_fetch_validates_source_and_keeps_secret_urls_private(self):
        for url in (
            "http://productdata.awin.com/secret",
            "https://productdata.awin.com.evil.org/secret",
            "https://127.0.0.1/secrets",
            "https://evil.test/csv",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    af.fetch_awin_feed(url)
        with self.assertRaises(ValueError):
            af._AwinRedirects().redirect_request(
                type("RequestStub",(),{"full_url":"https://productdata.awin.com/x"})(),
                None,302,"redirect",{},"https://evil.test/test")

    def test_sync_without_feed_does_nothing(self):
        with patch.object(af,"fetch_awin_feed") as download:
            result=af.run_sync(main.db,"")
        self.assertEqual(result["state"],"not_configured")
        download.assert_not_called()

    def test_sync_with_authorized_feed_updates_public_api_without_credentials(self):
        csv_text=sample_csv(product())
        secret="https://datafeed.api.productserve.com/datafeed/download/apikey/VERY_PRIVATE/fid/321"
        main.AWIN_FEED_URL=secret
        with patch.object(af,"fetch_awin_feed",return_value=csv_text) as fetch:
            synced=af.run_sync(main.db,main.AWIN_FEED_URL)
        self.assertEqual(synced["state"],"ok")
        self.assertEqual(synced["published"],1)
        fetch.assert_called_once_with(secret)
        server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("GET","/api/products")
            response=conn.getresponse()
            self.assertEqual(response.status,200)
            products=json.loads(response.read())["products"]
            self.assertEqual(len(products),1)
            self.assertEqual(products[0]["merchant"],"Loja Exemplo")
            self.assertFalse(products[0]["verified_discount"])
            conn.close()
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("GET","/api/admin/status",headers={"X-Admin-Key":"test-feed-secret"})
            response=conn.getresponse()
            body=response.read().decode("utf-8")
            self.assertEqual(response.status,200)
            self.assertNotIn("VERY_PRIVATE",body)
            self.assertEqual(json.loads(body)["affiliate_feed_published"],1)
            conn.close()
        finally:
            server.shutdown();server.server_close()

    def test_admin_only_sync_and_feed_url_never_taken_from_request(self):
        server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("POST","/api/admin/affiliate/sync",body='{"feed_url":"https://evil.test"}',
                         headers={"Content-Type":"application/json"})
            response=conn.getresponse()
            self.assertEqual(response.status,403);response.read();conn.close()
            with patch.object(af,"run_sync",return_value={"state":"not_configured"}) as synced:
                conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
                conn.request("POST","/api/admin/affiliate/sync",body='{"feed_url":"https://evil.test"}',
                             headers={"X-Admin-Key":"test-feed-secret","Content-Type":"application/json"})
                response=conn.getresponse()
                self.assertEqual(response.status,200);response.read();conn.close()
                synced.assert_called_once_with(main.db,"",main.PROMO_MIN)
        finally:
            server.shutdown();server.server_close()


if __name__ == "__main__":
    unittest.main()
