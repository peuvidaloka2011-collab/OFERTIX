"""Testes offline: nunca consultam o Mercado Livre nem usam credenciais reais."""
import http.client
import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class PromotionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = main.DB
        self.old_admin = main.ADMIN_KEY
        main.DB = Path(self.tmp.name) / "test.sqlite"
        main.ADMIN_KEY = "test-key-only"
        main.init_db()

    def tearDown(self):
        main.DB = self.old_db
        main.ADMIN_KEY = self.old_admin
        self.tmp.cleanup()

    def item(self, status="active"):
        return {"title": "Fone de ouvido", "status": status, "available_quantity": 50,
                "thumbnail": "https://http2.mlstatic.com/image.jpg", "permalink": "https://www.mercadolivre.com.br/item"}

    def sale(self, current=80, regular=100):
        return {"amount": current, "regular_amount": regular, "currency_id": "BRL"}

    def test_only_confirmed_discount(self):
        result = main.verified_promotion(self.item(), self.sale())
        self.assertEqual(result["discount_percent"], 20.0)
        self.assertIsNone(main.verified_promotion(self.item(), self.sale(90, 90)))
        self.assertIsNone(main.verified_promotion(self.item(), {"amount": 70, "currency_id": "BRL"}))
        self.assertIsNone(main.verified_promotion(self.item(status="paused"), self.sale()))
        self.assertIsNone(main.verified_promotion(self.item(), self.sale(92, 100), threshold=10))

    def test_catalog_diagnosis_reports_structure_without_secrets(self):
        requested = []
        def fake_api(path):
            requested.append(path)
            if path == "/categories/MLB1055":
                return {"name": "Celulares", "children_categories": []}
            if path == "/highlights/MLB/category/MLB1055":
                return {"content": [
                    {"id": "MLB7001", "type": "PRODUCT"},
                    {"id": "MLB7002", "type": "PRODUCT"},
                    {"id": "MLB7003", "type": "PRODUCT"},
                    {"id": "MLB7004", "type": "PRODUCT"},
                ]}
            if path == "/products/MLB7001":
                return {"status": "active", "buy_box_winner": {"item_id": "MLB8001"},
                        "children_ids": [], "access_token": "MUST_NOT_EXPOSE"}
            if path == "/products/MLB7002":
                return {"status": "inactive", "buy_box_winner": None,
                        "children_ids": ["MLB7999"], "parent_id": "MLB6000"}
            if path == "/products/MLB7003":
                return {"status": "active", "buy_box_winner": None,
                        "pickers": [{"products": [{"product_id": "MLB7003"}, {"product_id": "MLB7004"}]}]}
            raise AssertionError("unexpected path: " + path)
        with patch.object(main, "ml_api", side_effect=fake_api):
            result = main.diagnose_catalog("MLB1055")
        self.assertEqual(result["ranking_count"], 4)
        self.assertEqual(result["sample_size"], 3)
        self.assertTrue(result["sample"][0]["buy_box_winner_has_item_id"])
        self.assertEqual(result["sample"][1]["children_count"], 1)
        self.assertEqual(result["sample"][2]["picker_options_count"], 2)
        self.assertNotIn("MUST_NOT_EXPOSE", json.dumps(result))
        self.assertNotIn("/products/MLB7004", requested)

    def test_diagnosis_parent_category_skips_product_lookup(self):
        with patch.object(main, "ml_api", return_value={
            "name": "Categoria pai",
            "children_categories": [{"id": "MLB1234", "name": "Filho"}]
        }) as api:
            data = main.diagnose_catalog("MLB1051")
        self.assertFalse(data["is_final"])
        api.assert_called_once_with("/categories/MLB1051")

    def test_parent_category_shows_subcategories_instead_of_failing(self):
        def fake_api(path):
            self.assertEqual(path, "/categories/MLB1051")
            return {"name": "Celulares e Telefones", "children_categories": [
                {"id": "MLB10001", "name": "Celulares"},
                {"id": "MLB10002", "name": "Acessorios"}
            ]}
        with patch.object(main, "ml_api", side_effect=fake_api):
            result = main.import_category("MLB1051")
        self.assertTrue(result["requires_subcategory"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["subcategories"][0]["id"], "MLB10001")

    def test_catalog_product_resolves_buy_box_winner_and_checks_discount(self):
        paths = []
        def fake_api(path):
            paths.append(path)
            if path.startswith("/categories/"):
                return {"name":"Eletronicos", "children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"PRODUCT","id":"MLB900123","position":1}]}
            if path == "/products/MLB900123":
                return {"status":"active","buy_box_winner":{"item_id":"MLB55555"}}
            if path == "/items/MLB55555":
                return self.item()
            if path == "/items/MLB55555/sale_price?context=channel_marketplace":
                return self.sale()
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB432825")
        self.assertEqual(result["ranking_entries"],1)
        self.assertEqual(result["checked"],1)
        self.assertEqual(result["resolved_catalog"],1)
        self.assertEqual(result["promotions"],1)
        self.assertIn("/products/MLB900123",paths)
        with main.db() as conn:
            row=conn.execute("SELECT id,affiliate_url,is_promo FROM products").fetchone()
            self.assertEqual(row["id"],"MLB55555")
            self.assertIsNone(row["affiliate_url"])
            self.assertEqual(row["is_promo"],1)

    def test_user_product_resolves_seller_items_and_enforces_discount(self):
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name":"Acessorios", "children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"USER_PRODUCT","id":"MLBU123","position":1}]}
            if path=="/user-products/MLBU123":
                return {"id":"MLBU123","user_id":987654}
            if path=="/users/987654/items/search?user_product_id=MLBU123":
                return {"results":["MLB10101","MLB20202","MLB30303"]}
            if path.startswith("/items/") and "/sale_price" not in path:
                return self.item()
            if path.startswith("/items/MLB10101/sale_price"):
                return self.sale(80,100)
            if path.startswith("/items/MLB20202/sale_price"):
                return self.sale(100,100)
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB7466")
        self.assertEqual(result["checked"],2)
        self.assertEqual(result["promotions"],1)
        self.assertEqual(result["without_discount"],1)
        self.assertEqual(result["catalog_unresolved"],0)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM products").fetchone()[0],2)

    def test_product_with_no_buy_box_is_not_published(self):
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name":"Acessorios","children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"PRODUCT","id":"MLB987","position":1}]}
            if path=="/products/MLB987":
                return {"status":"active","buy_box_winner":None}
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB432825")
        self.assertEqual(result["checked"],0)
        self.assertEqual(result["catalog_unresolved"],1)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM products").fetchone()[0],0)

    def test_parent_product_uses_active_child_buy_box(self):
        paths = []
        def fake_api(path):
            paths.append(path)
            if path.startswith("/categories/"):
                return {"name":"Celulares","children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"PRODUCT","id":"MLB90000","position":1}]}
            if path=="/products/MLB90000":
                return {"status":"inactive","buy_box_winner":None,"children_ids":["MLB90100","MLB90200"]}
            if path=="/products/MLB90100":
                return {"status":"active","buy_box_winner":{"item_id":"MLB99991"},"children_ids":[]}
            if path=="/products/MLB90200":
                return {"status":"active","buy_box_winner":None,"children_ids":[]}
            if path.startswith("/items/") and "/sale_price" not in path:
                return self.item()
            if path=="/items/MLB99991/sale_price?context=channel_marketplace":
                return self.sale()
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB1055")
        self.assertEqual(result["checked"],1)
        self.assertEqual(result["promotions"],1)
        self.assertEqual(result["resolved_catalog"],1)
        self.assertIn("/products/MLB90100",paths)

    def test_item_with_mlbu_id_is_resolved(self):
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name":"Acessorios","children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"ITEM","id":"MLBU5001","position":1}]}
            if path=="/user-products/MLBU5001":
                return {"user_id":555}
            if path=="/users/555/items/search?user_product_id=MLBU5001":
                return {"results":["MLB100100"]}
            if path=="/items/MLB100100":
                return self.item()
            if path=="/items/MLB100100/sale_price?context=channel_marketplace":
                return self.sale()
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB1055")
        self.assertEqual(result["checked"],1)
        self.assertEqual(result["promotions"],1)
        self.assertEqual(result["item_mlbu_entries"],1)

    def test_parent_without_valid_child_is_not_published(self):
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name":"Acessorios","children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"PRODUCT","id":"MLB90000","position":1}]}
            if path=="/products/MLB90000":
                return {"status":"inactive","buy_box_winner":None,
                        "children_ids":["MLB90200"]}
            if path=="/products/MLB90200":
                return {"status":"active","buy_box_winner":None,
                        "children_ids":[]}
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB1055")
        self.assertEqual(result["checked"],0)
        self.assertEqual(result["catalog_unresolved"],1)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_catalog_permission_error_is_reported_without_false_items(self):
        from urllib.error import HTTPError
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name":"Acessorios", "children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"USER_PRODUCT","id":"MLBU123","position":1}]}
            if path=="/user-products/MLBU123":
                raise HTTPError("https://api.mercadolibre.com",403,"Forbidden",{},None)
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result=main.import_category("MLB432825")
        self.assertEqual(result["checked"],0)
        self.assertEqual(result["catalog_errors"],1)
        self.assertIn("403",result["diagnostics"][0])

    def test_final_category_imports_verified_item(self):
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name": "Fones", "children_categories": []}
            if path.startswith("/highlights/"):
                return {"content": [{"type": "ITEM", "id": "MLB55555", "position": 1}]}
            if path.endswith("/sale_price?context=channel_marketplace"):
                return self.sale()
            if path.startswith("/items/"):
                return self.item()
            raise AssertionError(path)
        with patch.object(main, "ml_api", side_effect=fake_api):
            result = main.import_category("MLB432825")
        self.assertEqual(result["promotions"], 1)
        self.assertEqual(result["checked"], 1)

    def test_valid_affiliate_domains(self):
        self.assertTrue(main.allowed_affiliate_link("https://mercadolivre.com/sec/abc"))
        self.assertTrue(main.allowed_affiliate_link("https://meli.la/abc"))
        self.assertFalse(main.allowed_affiliate_link("https://mercadolivre.com.evil.org/abc"))
        self.assertFalse(main.allowed_affiliate_link("http://mercadolivre.com/sec/abc"))

    def test_offer_not_visible_without_link(self):
        fake = lambda path: self.item() if "/sale_price" not in path else self.sale()
        with patch.object(main, "ml_api", side_effect=fake):
            self.assertTrue(main.verify_and_save("MLB123456", "MLB1051", 2))
        with main.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM products WHERE is_promo=1").fetchone()[0], 1)
        server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/api/products")
            response = conn.getresponse()
            self.assertEqual(json.loads(response.read())["products"], [])
            conn.close()
            with main.db() as c:
                c.execute("UPDATE products SET affiliate_url='https://mercadolivre.com/sec/abc' WHERE id='MLB123456'")
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/api/products")
            response = conn.getresponse()
            data = json.loads(response.read())["products"]
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["discount_percent"], 20.0)
            conn.close()
            with patch.object(main, "ml_api", side_effect=lambda path: self.item() if "/sale_price" not in path else self.sale(100, 100)):
                self.assertFalse(main.verify_and_save("MLB123456", "MLB1051", 2))
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/api/products")
            self.assertEqual(json.loads(conn.getresponse().read())["products"], [])
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
