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

    def test_final_category_counts_catalog_types_without_inventing_offers(self):
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name": "Fones", "children_categories": []}
            if path.startswith("/highlights/"):
                return {"content": [
                    {"type": "USER_PRODUCT", "id": "MLBU123", "position": 1},
                    {"type": "PRODUCT", "id": "MLB987", "position": 2}
                ]}
            raise AssertionError("Nao deveria consultar itens de catalogo sem resolucao: " + path)
        with patch.object(main, "ml_api", side_effect=fake_api):
            result = main.import_category("MLB432825")
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["catalog_types_not_supported"], 2)
        self.assertIn("catalogo", result["message"])
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)

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
