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

    def test_google_feed_adds_all_carraro_separately_from_promotions(self):
        from affiliate_feeds import parse_google_csv
        import csv
        import io
        source={
            "advertiser_id":"81383","advertiser_name":"Carraro BR","id":"sku-1",
            "title":"Conjunto de cadeiras para jantar","price":"333.32 BRL",
            "sale_price":"","availability":"in_stock",
            "aw_deep_link":"https://www.awin1.com/cread.php?awinmid=81383&awinaffid=100",
            "image_link":"https://imagens.example.com/cadeiras.jpg",
            "product_type":"","google_product_category":"","adult":"",
        }
        buffer=io.StringIO()
        writer=csv.DictWriter(buffer,fieldnames=list(source))
        writer.writeheader()
        writer.writerow(source)
        writer.writerow({**source,"id":"sku-2","availability":"out_of_stock"})
        writer.writerow({**source,"id":"sku-3","price":"300.00 BRL",
                         "sale_price":"210.00 BRL"})
        writer.writerow({**source,"id":"sku-4","price":"100.00 BRL",
                         "sale_price":"95.00 BRL"})
        parsed=parse_google_csv(buffer.getvalue(),minimum_discount=10)
        self.assertEqual(parsed["scanned"],4)
        self.assertEqual(len(parsed["offers"]),1)
        self.assertEqual(len(parsed["carraro_products"]),4)
        self.assertEqual(sum(x["in_stock"] for x in parsed["carraro_products"]),3)
        self.assertEqual(parsed["offers"][0]["discount_percent"],30)
        with patch("scripts.sync_awin_catalog.fetch_awin_feed",return_value=buffer.getvalue()):
            generated,_=generate("https://productdata.awin.com/datafeed/x")
        self.assertEqual(len(generated["products"]),1)
        self.assertEqual(len(generated["carraro_products"]),4)
        self.assertNotIn("partner_products",generated)
        self.assertEqual(len(pac.read_catalog(generated)),1)
        self.assertEqual(len(pac.read_carraro_catalog(generated)),4)

    def test_google_products_reject_usd_adults_and_no_links(self):
        from affiliate_feeds import parse_google_csv
        import io
        import csv
        source={"advertiser_id":"81383","advertiser_name":"Carraro BR","id":"x",
                "title":"Mesa cozinha dobrável", "price":"500.00 BRL",
                "sale_price":"","availability":"in_stock",
                "aw_deep_link":"https://www.awin1.com/cread.php?x=1",
                "image_link":"https://cdn.example.org/mesa.jpg"}
        bad=[
            {**source,"id":"1","price":"500.00 USD"},
            {**source,"id":"2","adult":"yes"},
            {**source,"id":"3","aw_deep_link":"https://example.org/item"},
            {**source,"id":"4","availability":"out_of_stock"},
            {**source,"id":"5","sale_price":"60.00 USD"},
        ]
        buf=io.StringIO()
        writer=csv.DictWriter(buf,fieldnames=sorted({k for r in bad for k in r}))
        writer.writeheader()
        for row in bad:writer.writerow(row)
        parsed=parse_google_csv(buf.getvalue())
        self.assertEqual(parsed["offers"],[])


    def test_carraro_without_discount_still_appears_in_normal_catalog(self):
        from affiliate_feeds import parse_google_csv
        import csv
        import io
        header=["advertiser_id","advertiser_name","id","title","price","sale_price",
                "availability","aw_deep_link","image_link"]
        rows=[{"advertiser_id":"81383","advertiser_name":"Carraro BR","id":str(i),
               "title":"Mesa para cozinha moderna","price":"549.90 BRL",
               "sale_price":"","availability":"in_stock",
               "aw_deep_link":"https://www.awin1.com/cread.php?awinmid=81383&awinaffid=111",
               "image_link":"https://cdn.example.org/mesa.jpg"}
              for i in range(5)]
        buf=io.StringIO()
        writer=csv.DictWriter(buf,fieldnames=header)
        writer.writeheader();writer.writerows(rows)
        parsed=parse_google_csv(buf.getvalue())
        self.assertEqual(parsed["scanned"],5)
        self.assertEqual(parsed["offers"],[])
        self.assertEqual(len(parsed["carraro_products"]),5)
        with patch("scripts.sync_awin_catalog.fetch_awin_feed",return_value=buf.getvalue()):
            result,scanned=generate("https://productdata.awin.com/datafeed/download/fid/81383")
        self.assertEqual(scanned,5)
        self.assertEqual(result["products"],[])
        self.assertEqual(len(result["carraro_products"]),5)
        self.assertNotIn("partner_products",result)
        self.assertEqual(pac.read_catalog(result),[])
        self.assertEqual(len(pac.read_carraro_catalog(result)),5)

    def test_carraro_public_api_returns_products_without_fake_discounts(self):
        sample=catalog()
        sample["products"]=[]
        sample["carraro_products"]=[
            {"id":"carraro1","merchant":"Carraro BR","title":"Mesa Carraro dobravel",
             "category":"Móveis e Casa","price":299.99,
             "image":"https://cdn.example.org/carraro.jpg",
             "affiliate_url":"https://www.awin1.com/cread.php?x=123",
             "in_stock":True},
            {"id":"carraro2","merchant":"Carraro BR","title":"Mesa Carraro esgotada",
             "category":"Móveis e Casa","price":199.99,
             "image":"https://cdn.example.org/carraro2.jpg",
             "affiliate_url":"https://www.awin1.com/cread.php?x=234",
             "in_stock":False},
        ]
        with tempfile.TemporaryDirectory() as d:
            old_db=main.DB
            main.DB=Path(d)/"test.sqlite"
            main.init_db()
            with patch.object(pac,"get_carraro_products",
                              return_value=pac.read_carraro_catalog(sample)):
                server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                try:
                    conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=4)
                    conn.request("GET","/api/carraro-products")
                    response=conn.getresponse()
                    data=json.loads(response.read())
                    self.assertEqual(response.status,200)
                    self.assertEqual(data["count"],2)
                    self.assertTrue(data["products"][0]["in_stock"])
                    self.assertFalse(data["products"][1]["in_stock"])
                    self.assertNotIn("discount_percent",data["products"][0])
                    conn.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    main.DB=old_db

    def test_http_failure_returns_expired_cache_as_empty(self):
        pac._CACHE.update(at=0.0,offers=[],expires_at=None)
        with patch("public_affiliate_catalog.urllib.request.urlopen",
                   side_effect=OSError("Network unavailable")):
            self.assertEqual(pac.get_offers(),[])


if __name__=="__main__":
    unittest.main()
