"""Testes offline: nunca consultam o Mercado Livre nem usam credenciais reais."""
import http.client
import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
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

    def manual_offer(self, **changes):
        data = {
            "title": "Caixa de Som AIWA Boombox Plus",
            "category": "Eletronicos",
            "price": 1471.00,
            "original_price": 2299.00,
            "affiliate_url": "https://meli.la/ProdutoOficial",
            "image": "https://http2.mlstatic.com/foto-produto.jpg",
            "starts_at": None,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "confirmed_by_admin": True,
        }
        data.update(changes)
        return data

    def test_v12_manual_offer_without_oauth_appears_as_unverified(self):
        with patch.object(main, "ml_api") as api:
            created=main.save_manual_offer(self.manual_offer())
        api.assert_not_called()
        self.assertTrue(created["ok"])
        self.assertEqual(created["pricing_source"], "manual")
        with main.db() as conn:
            listed=main.visible_manual_offers(conn)
        self.assertEqual(len(listed),1)
        self.assertAlmostEqual(listed[0]["discount_percent"],36.02,places=1)
        self.assertEqual(listed[0]["title"],"Caixa de Som AIWA Boombox Plus")
        server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("GET","/api/products")
            response=conn.getresponse()
            self.assertEqual(response.status,200)
            data=json.loads(response.read())
            conn.close()
            self.assertEqual(len(data["products"]),1)
            offer=data["products"][0]
            self.assertEqual(offer["pricing_source"],"manual")
            self.assertFalse(offer["verified_discount"])
            self.assertEqual(offer["affiliate_url"],"https://meli.la/ProdutoOficial")
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("GET","/api/admin/manual/offers")
            response=conn.getresponse()
            self.assertEqual(response.status,403)
            response.read();conn.close()
        finally:
            server.shutdown();server.server_close()

    def test_v12_manual_offer_schedule_and_expiry_without_background_worker(self):
        now=datetime.now(timezone.utc)
        start=(now+timedelta(hours=1)).isoformat()
        end=(now+timedelta(hours=2)).isoformat()
        result=main.save_manual_offer(self.manual_offer(starts_at=start,expires_at=end))
        with main.db() as conn:
            self.assertEqual(main.visible_manual_offers(conn),[])
            conn.execute("UPDATE manual_offers SET starts_at=? WHERE id=?",
                ((now-timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),result["id"]))
        with main.db() as conn:
            self.assertEqual(len(main.visible_manual_offers(conn)),1)
            conn.execute("UPDATE manual_offers SET expires_at=? WHERE id=?",
                ((now-timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),result["id"]))
        with main.db() as conn:
            self.assertEqual(main.visible_manual_offers(conn),[])

    def test_v12_manual_offer_pausing_and_resuming(self):
        result=main.save_manual_offer(self.manual_offer())
        main.change_manual_offer({"id":result["id"],"active":False})
        with main.db() as conn:self.assertEqual(main.visible_manual_offers(conn),[])
        main.change_manual_offer({"id":result["id"],"active":True})
        with main.db() as conn:self.assertEqual(len(main.visible_manual_offers(conn)),1)

    def test_v12_manual_offer_can_be_edited_and_resets_schedule(self):
        result=main.save_manual_offer(self.manual_offer())
        changed=self.manual_offer(
            id=result["id"],price=1300.0,original_price=2500.0,
            expires_at=(datetime.now(timezone.utc)+timedelta(hours=3)).isoformat())
        main.save_manual_offer(changed)
        with main.db() as conn:
            row=conn.execute("SELECT * FROM manual_offers WHERE id=?",(result["id"],)).fetchone()
            self.assertEqual(row["price"],1300.0)
            self.assertEqual(row["discount_percent"],48.0)
            self.assertEqual(conn.execute("SELECT count(*) FROM manual_offers").fetchone()[0],1)

    def test_v12_manual_rejects_low_discount_false_prices_and_long_expiration(self):
        cases=[
            {"price":2000,"original_price":2100},
            {"price":2300,"original_price":2299},
            {"price":"nan"},
            {"price":-1},
            {"price":True},
            {"expires_at":(datetime.now(timezone.utc)+timedelta(days=5)).isoformat()},
            {"expires_at":(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()},
            {"confirmed_by_admin":False},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    main.save_manual_offer(self.manual_offer(**case))
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM manual_offers").fetchone()[0],0)

    def test_v12_rejects_affiliate_profile_and_untrusted_links(self):
        bad=[
            "https://www.mercadolivre.com.br/",
            "https://www.mercadolivre.com.br/social/conta",
            "https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true",
            "https://meli.la",
            "http://meli.la/abc",
            "https://meli.la.evil.com/test",
            "https://evil.com/p/MLB1234567890",
        ]
        for link in bad:
            with self.subTest(link=link):
                with self.assertRaises(ValueError):
                    main.save_manual_offer(self.manual_offer(affiliate_url=link))

    def test_v12_manual_rejects_invalid_image_and_accepts_compressed_photo(self):
        import base64
        for image in [
            "https://localhost/secret.jpg",
            "https://mlstatic.com.evil.com/foto.png",
            "data:image/png;base64,bm90aW1hZ2U=",
            "data:text/html;base64,PHNjcmlwdD4=",
            "https://127.0.0.1/picture.png",
        ]:
            with self.subTest(image=image):
                with self.assertRaises(ValueError):
                    main.save_manual_offer(self.manual_offer(image=image))
        sample=base64.b64encode(b"\xff\xd8\xff"+b"a"*45).decode("ascii")
        result=main.save_manual_offer(self.manual_offer(image="data:image/jpeg;base64,"+sample))
        with main.db() as conn:
            row=conn.execute("SELECT image FROM manual_offers WHERE id=?",(result["id"],)).fetchone()
        self.assertTrue(row["image"].startswith("data:image/jpeg;base64,"))

    def test_v12_manual_routes_require_admin_and_no_oauth(self):
        server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        body=json.dumps(self.manual_offer())
        try:
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("POST","/api/admin/manual/save",body=body,headers={"Content-Type":"application/json"})
            response=conn.getresponse()
            self.assertEqual(response.status,403);response.read();conn.close()
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("POST","/api/admin/manual/save",body=body,
                         headers={"X-Admin-Key":"test-key-only","Content-Type":"application/json"})
            response=conn.getresponse()
            self.assertEqual(response.status,200)
            created=json.loads(response.read());conn.close()
            self.assertTrue(created["ok"])
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("GET","/api/admin/status",headers={"X-Admin-Key":"test-key-only"})
            response=conn.getresponse()
            self.assertEqual(response.status,200)
            data=json.loads(response.read())
            self.assertEqual(data["manual_published"],1)
            self.assertEqual(data["published"],1)
            response.read();conn.close()
        finally:
            server.shutdown();server.server_close()

    def test_only_confirmed_discount(self):
        result = main.verified_promotion(self.item(), self.sale())
        self.assertEqual(result["discount_percent"], 20.0)
        self.assertIsNone(main.verified_promotion(self.item(), self.sale(90, 90)))
        self.assertIsNone(main.verified_promotion(self.item(), {"amount": 70, "currency_id": "BRL"}))
        self.assertIsNone(main.verified_promotion(self.item(status="paused"), self.sale()))
        self.assertIsNone(main.verified_promotion(self.item(), self.sale(92, 100), threshold=10))

    def test_v11_diagnosis_checks_item_and_price_independently(self):
        from io import BytesIO
        from urllib.error import HTTPError
        calls = []
        def fake_api(path):
            calls.append(path)
            if path == "/items/MLB6499468038":
                return self.item()
            if path == "/items/MLB6499468038/sale_price?context=channel_marketplace":
                raise HTTPError(path, 403, "Forbidden", {},
                                BytesIO(b'{"error":"Invalid scopes","code":"FORBIDDEN","access_token":"SECRET_X"}'))
            raise AssertionError("unexpected query " + path)
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=fake_api):
                result=main.diagnose_item_and_price("MLB6499468038")
        self.assertEqual(len(calls),2)
        self.assertEqual(result["item"]["state"],"ok")
        self.assertEqual(result["sale_price"]["http_status"],403)
        self.assertEqual(result["sale_price"]["reason"],"escopos_insuficientes")
        self.assertFalse(result["can_publish"])
        self.assertFalse(result["promotion_conditions_met"])
        self.assertNotIn("SECRET_X",json.dumps(result))
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_v11_diagnosis_price_can_succeed_despite_item_403(self):
        from urllib.error import HTTPError
        from io import BytesIO
        paths = []
        def fake_api(path):
            paths.append(path)
            if path == "/items/MLB6499468038":
                raise HTTPError(path,403,"Forbidden",{},BytesIO(
                    b'{"error":"access_denied","message":"Sensitive TOKEN_123987"}'))
            if path.endswith("/sale_price?context=channel_marketplace"):
                return self.sale(70,100)
            raise AssertionError(path)
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=fake_api):
                result=main.diagnose_item_and_price("MLB6499468038")
        self.assertEqual(len(paths),2)
        self.assertEqual(result["item"]["state"],"http_error")
        self.assertEqual(result["item"]["reason"],"acesso_restrito")
        self.assertEqual(result["sale_price"]["amount"],70.0)
        self.assertEqual(result["sale_price"]["discount_percent"],30.0)
        self.assertTrue(result["sale_price"]["minimum_discount_met"])
        self.assertFalse(result["promotion_conditions_met"])
        self.assertFalse(result["can_publish"])
        self.assertNotIn("TOKEN_123987",json.dumps(result))
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_v11_diagnosis_both_work_does_not_publish_or_save(self):
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=lambda path:
                    self.sale(80,100) if "/sale_price?" in path else self.item()):
                result=main.diagnose_item_and_price("MLB6499468038")
        self.assertTrue(result["promotion_conditions_met"])
        self.assertFalse(result["can_publish"])
        self.assertEqual(result["item"]["http_status"],200)
        self.assertEqual(result["sale_price"]["http_status"],200)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_v11_diagnosis_without_oauth_does_not_call_api(self):
        with patch.object(main,"TOKEN",""):
            with patch.object(main.oauth_ml,"connected",return_value=False):
                with patch.object(main,"ml_api") as ml:
                    result=main.diagnose_item_and_price("MLB6499468038")
        self.assertFalse(result["connected"])
        self.assertFalse(result["can_publish"])
        self.assertEqual(result["item"]["reason"],"oauth_pendente")
        ml.assert_not_called()

    def test_v11_diagnosis_invalid_ids_rejected(self):
        for bad in ("", "MLBU12345", "MLBxxx", "MLB123;DELETE"):
            with self.subTest(id=bad):
                with self.assertRaises(ValueError):
                    main.diagnose_item_and_price(bad)

    def test_v11_diagnosis_missing_regular_price_not_promotion(self):
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=lambda path:
                    {"amount":70,"regular_amount":None,"currency_id":"BRL"}
                    if "/sale_price?" in path else self.item()):
                result=main.diagnose_item_and_price("MLB6499468038")
        self.assertIsNone(result["sale_price"]["discount_percent"])
        self.assertFalse(result["sale_price"]["minimum_discount_met"])
        self.assertFalse(result["promotion_conditions_met"])

    def test_v11_diagnosis_no_stock_does_not_claim_promotion(self):
        without_stock=self.item()
        without_stock["available_quantity"]=0
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=lambda path:
                    self.sale() if "/sale_price?" in path else without_stock):
                result=main.diagnose_item_and_price("MLB6499468038")
        self.assertFalse(result["item"]["stock_confirmed"])
        self.assertFalse(result["promotion_conditions_met"])

    def test_v11_diagnostic_endpoint_is_admin_only(self):
        server=main.ThreadingHTTPServer(("127.0.0.1",0),main.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            payload=json.dumps({"id":"MLB6499468038"})
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            conn.request("POST","/api/admin/item/diagnose",body=payload,
                         headers={"Content-Type":"application/json"})
            response=conn.getresponse()
            self.assertEqual(response.status,403)
            response.read()
            conn.close()
            with patch.object(main,"diagnose_item_and_price",return_value={
                "id":"MLB6499468038","item":{"state":"ok"},
                "sale_price":{"state":"http_error","http_status":403},
                "can_publish":False
            }) as diagnose:
                conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
                conn.request("POST","/api/admin/item/diagnose",body=payload,
                    headers={"X-Admin-Key":"test-key-only","Content-Type":"application/json"})
                response=conn.getresponse()
                self.assertEqual(response.status,200)
                data=json.loads(response.read())
                self.assertFalse(data["can_publish"])
                diagnose.assert_called_once_with("MLB6499468038")
                conn.close()
        finally:
            server.shutdown()
            server.server_close()

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
            if path.startswith("/sites/MLB/search?category="):
                return {"results": []}
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
            if path.startswith("/sites/MLB/search?category="):
                return {"results": []}
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
            if path.startswith("/sites/MLB/search?category="):
                return {"results": []}
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

    def test_category_search_fallback_finds_listings_when_ranking_has_no_winner(self):
        calls = []
        def fake_api(path):
            calls.append(path)
            if path == "/categories/MLB1055":
                return {"name": "Celulares", "children_categories": []}
            if path == "/highlights/MLB/category/MLB1055":
                return {"content": [{"type": "PRODUCT", "id": "MLB1009"}]}
            if path == "/products/MLB1009":
                return {"status": "active", "buy_box_winner": None, "pickers": []}
            if path == "/sites/MLB/search?category=MLB1055&limit=20":
                return {"results": [
                    {"id": "MLB8080"}, {"id": "MLB8080"}, {"id": "MLB9090"},
                    {"id": "INVALID"}
                ]}
            if path in ("/items/MLB8080", "/items/MLB9090"):
                return self.item()
            if path == "/items/MLB8080/sale_price?context=channel_marketplace":
                return self.sale(70, 100)
            if path == "/items/MLB9090/sale_price?context=channel_marketplace":
                return self.sale(98, 100)
            raise AssertionError(path)
        with patch.object(main, "ml_api", side_effect=fake_api):
            result = main.import_category("MLB1055")
        self.assertTrue(result["category_search_attempted"])
        self.assertEqual(result["category_search_returned"], 2)
        self.assertEqual(result["category_search_checked"], 2)
        self.assertEqual(result["promotions"], 1)
        self.assertEqual(result["without_discount"], 1)
        self.assertIn("NAO necessariamente mais vendidos", result["message"])
        self.assertEqual(calls.count("/items/MLB8080"), 1)
        with main.db() as conn:
            rows = conn.execute("SELECT id,is_promo,affiliate_url FROM products ORDER BY id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(row["is_promo"] for row in rows), 1)
            self.assertTrue(all(row["affiliate_url"] is None for row in rows))

    def test_category_search_fallback_handles_403_without_false_promotion(self):
        from urllib.error import HTTPError
        def fake_api(path):
            if path.startswith("/categories/"):
                return {"name":"Celulares","children_categories":[]}
            if path.startswith("/highlights/"):
                return {"content":[{"type":"PRODUCT","id":"MLB800"}]}
            if path == "/products/MLB800":
                return {"status":"active","buy_box_winner":None}
            if path.startswith("/sites/MLB/search?"):
                raise HTTPError(path,403,"Forbidden",{},None)
            raise AssertionError(path)
        with patch.object(main,"ml_api",side_effect=fake_api):
            result = main.import_category("MLB1055")
        self.assertEqual(result["checked"],0)
        self.assertEqual(result["promotions"],0)
        self.assertEqual(result["category_search_error"], "Busca por categoria: HTTP 403")

    def test_valid_affiliate_domains(self):
        self.assertTrue(main.allowed_affiliate_link("https://mercadolivre.com/sec/abc"))
        self.assertTrue(main.allowed_affiliate_link("https://meli.la/abc"))
        self.assertFalse(main.allowed_affiliate_link("https://mercadolivre.com.evil.org/abc"))
        self.assertFalse(main.allowed_affiliate_link("http://mercadolivre.com/sec/abc"))

    def test_catalog_url_finds_associated_listing_ids_without_inserting_offers(self):
        calls=[]
        def fake_api(path):
            calls.append(path)
            if path=="/products/MLB9876543210":
                return {
                    "id":"MLB9876543210","status":"active",
                    "buy_box_winner":None,
                    "children_ids":[],
                    "pickers":[{"products":[
                        {"product_id":"MLB900001"},
                        {"product_id":"MLB900002"},
                        {"product_id":"MLB900003"}
                    ]}]
                }
            if path=="/products/MLB9876543210/items":
                return {"results":[
                    {"item_id":"MLB8888888888","status":"active"},
                    {"item_id":"MLB9999999999","status":"active"},
                    {"item_id":"MLB8888888888","status":"active"},
                    {"item_id":"MLB2222222222","status":"paused"}
                ]}
            if path=="/products/MLB900001/items":
                return {"results":[{"item_id":"MLB7777777777","status":"active"}]}
            if path=="/products/MLB900002/items":
                return {"results":[]}
            raise AssertionError(path)
        url="https://www.mercadolivre.com.br/camera-de-video/p/MLB9876543210"
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=fake_api):
                result=main.find_catalog_listing_candidates(url)
        self.assertEqual(result["catalog_id"],"MLB9876543210")
        self.assertFalse(result["restricted"])
        self.assertEqual([x["id"] for x in result["candidates"]],
                         ["MLB8888888888","MLB9999999999","MLB7777777777"])
        self.assertEqual(len(calls),4)
        self.assertFalse(any("/items/MLB" in c for c in calls))
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_catalog_shortlink_redirect_is_validated(self):
        url="https://meli.la/test"
        destination="https://www.mercadolivre.com.br/camera/p/MLB1111111111?source=afiliado"
        with patch.object(main,"affiliate_redirect_destination",return_value=destination) as redirect:
            with patch.object(main.oauth_ml,"connected",return_value=False):
                result=main.find_catalog_listing_candidates(url)
        redirect.assert_called_once_with(url)
        self.assertEqual(result["catalog_id"],"MLB1111111111")
        self.assertTrue(result["restricted"])
        self.assertEqual(result["candidates"],[])

    def test_catalog_candidates_obey_403_without_trying_siblings(self):
        from urllib.error import HTTPError
        calls=[]
        def fake_api(path):
            calls.append(path)
            if path=="/products/MLB9876543210":
                return {"status":"active","pickers":[{"products":[{"product_id":"MLB900001"}]}]}
            if path=="/products/MLB9876543210/items":
                raise HTTPError(path,403,"Forbidden",{},None)
            raise AssertionError("No permission bypass: "+path)
        url="https://www.mercadolivre.com.br/p/MLB9876543210"
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=fake_api):
                result=main.find_catalog_listing_candidates(url)
        self.assertTrue(result["restricted"])
        self.assertIn("HTTP 403",result["message"])
        self.assertEqual(result["candidates"],[])
        self.assertEqual(calls,[
            "/products/MLB9876543210",
            "/products/MLB9876543210/items"
        ])

    def test_catalog_candidates_do_not_trust_redirects_to_external_hosts(self):
        with patch.object(main,"affiliate_redirect_destination",
                          return_value="https://evil.example/p/MLB1234567890") as redirect:
            with self.assertRaisesRegex(ValueError,"nao permitido"):
                main.find_catalog_listing_candidates("https://meli.la/xyz")
        redirect.assert_called_once()
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_catalog_candidates_do_not_use_fake_id_in_querystring(self):
        url="https://www.mercadolivre.com.br/ofertas?catalog_id=MLB1111111111"
        with patch.object(main,"affiliate_redirect_destination",return_value=None):
            with self.assertRaisesRegex(ValueError,"Nao foi encontrada"):
                main.find_catalog_listing_candidates(url)

    def test_catalog_candidates_handles_catalog_api_refusal(self):
        from urllib.error import HTTPError
        url="https://www.mercadolivre.com.br/p/MLB9876543210"
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",
                    side_effect=HTTPError(url,403,"Forbidden",{},None)):
                result=main.find_catalog_listing_candidates(url)
        self.assertTrue(result["restricted"])
        self.assertEqual(result["candidates"],[])
        self.assertIn("HTTP 403",result["message"])

    def test_catalog_candidates_reports_selected_winner_without_promotion(self):
        url="https://www.mercadolivre.com.br/p/MLB9876543210"
        def fake_api(path):
            if path=="/products/MLB9876543210":
                return {"status":"active",
                        "buy_box_winner":{"item_id":"MLB1234567890"},
                        "children_ids":[]}
            if path=="/products/MLB9876543210/items":
                return {"results":[{"item_id":"MLB1234567890","status":"active"}]}
            raise AssertionError(path)
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=fake_api):
                result=main.find_catalog_listing_candidates(url)
        self.assertEqual(len(result["candidates"]),1)
        self.assertEqual(result["candidates"][0]["id"],"MLB1234567890")
        self.assertEqual(result["candidates"][0]["status"],"preco nao verificado")
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_link_shortener_auto_detects_listing_and_checks_discount(self):
        url="https://meli.la/2a1TWtest"
        target="https://produto.mercadolivre.com.br/MLB-1234567890-exemplo-_JM"
        def redirected(value):
            self.assertEqual(value,url)
            return target
        with patch.object(main,"affiliate_redirect_destination",side_effect=redirected) as follow:
            with patch.object(main.oauth_ml,"connected",return_value=True):
                with patch.object(main,"ml_api",side_effect=lambda path:
                        self.sale() if "/sale_price" in path else self.item()) as api:
                    result=main.register_affiliate_item("",url)
        self.assertEqual(result["id"],"MLB1234567890")
        self.assertEqual(result["detection"],"automatico")
        self.assertTrue(result["promotion_confirmed"])
        self.assertTrue(result["publicly_visible"])
        self.assertEqual(follow.call_count,1)
        self.assertEqual(api.call_count,2)
        with main.db() as conn:
            row=conn.execute(
                "SELECT affiliate_url,discount_percent,is_promo FROM products WHERE id=?",
                ("MLB1234567890",)
            ).fetchone()
        self.assertEqual(row["affiliate_url"],url)
        self.assertEqual(row["discount_percent"],20.0)
        self.assertEqual(row["is_promo"],1)

    def test_direct_listing_url_needs_no_redirect_request(self):
        url="https://www.mercadolivre.com.br/MLB-9876543210-fone-de-ouvido-_JM?affiliate=123"
        with patch.object(main,"affiliate_redirect_destination") as follow:
            self.assertEqual(main.find_affiliate_listing_id(url),"MLB9876543210")
        follow.assert_not_called()

    def test_catalog_page_is_not_confused_with_listing(self):
        url="https://meli.la/example"
        catalog="https://www.mercadolivre.com.br/p/MLB9876543210?pdp_filters=sku"
        with patch.object(main,"affiliate_redirect_destination",return_value=catalog):
            with self.assertRaisesRegex(ValueError,"catalogo"):
                main.register_affiliate_item("",url)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_auto_id_from_querystring_is_not_used(self):
        url="https://www.mercadolivre.com.br/p/MLB9876543210?item_id=MLB1234567890"
        self.assertIsNone(main.listing_id_from_url(url))
        with patch.object(main,"affiliate_redirect_destination") as follow:
            with self.assertRaisesRegex(ValueError,"catalogo"):
                main.find_affiliate_listing_id(url)
        follow.assert_not_called()

    def test_external_redirect_is_not_followed_or_saved(self):
        url="https://meli.la/redirect"
        with patch.object(main,"affiliate_redirect_destination",return_value="http://127.0.0.1:8080/private") as follow:
            with self.assertRaisesRegex(ValueError,"outro dominio"):
                main.register_affiliate_item("",url)
        self.assertEqual(follow.call_count,1)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_official_to_untrusted_redirect_is_blocked(self):
        url="https://meli.la/redirect"
        with patch.object(main,"affiliate_redirect_destination",return_value="https://www.mercadolivre.com.br.evil.org/item"):
            with self.assertRaisesRegex(ValueError,"outro dominio"):
                main.find_affiliate_listing_id(url)

    def test_redirect_loop_is_limited(self):
        url="https://meli.la/loop"
        with patch.object(main,"affiliate_redirect_destination",return_value=url) as follow:
            with self.assertRaisesRegex(ValueError,"repetido"):
                main.find_affiliate_listing_id(url)
        self.assertEqual(follow.call_count,1)

    def test_shortlink_without_individual_listing_explains_manual_option(self):
        url="https://meli.la/single"
        with patch.object(main,"affiliate_redirect_destination",return_value=None):
            with self.assertRaisesRegex(ValueError,"Opcoes avancadas"):
                main.register_affiliate_item("",url)

    def test_explicit_manual_id_still_works_on_catalog_link(self):
        url="https://www.mercadolivre.com.br/p/MLB9876543210"
        with patch.object(main,"affiliate_redirect_destination") as follow:
            with patch.object(main.oauth_ml,"connected",return_value=False):
                result=main.register_affiliate_item("MLB1234567890",url)
        self.assertEqual(result["id"],"MLB1234567890")
        self.assertEqual(result["detection"],"manual")
        self.assertFalse(result["publicly_visible"])
        follow.assert_not_called()

    def test_conflicting_manual_id_is_rejected_for_direct_listing(self):
        url="https://produto.mercadolivre.com.br/MLB-1234567890-x-_JM"
        with self.assertRaisesRegex(ValueError,"nao corresponde"):
            main.register_affiliate_item("MLB2222222222",url)

    def test_link_validation_prevents_untrusted_hosts_and_ports(self):
        urls=[
            "http://meli.la/test", "https://meli.la.evil.org/test",
            "https://meli.la:8443/test", "https://meli.la@evil.com/MLB-1234567890",
            "https://127.0.0.1/MLB-1234567890",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(main.allowed_affiliate_link(url))
                with self.assertRaises(ValueError):
                    main.find_affiliate_listing_id(url)

    def test_manual_link_registers_verified_discount_without_category_import(self):
        calls = []
        def fake_api(path):
            calls.append(path)
            if path == "/items/MLB123456":
                return self.item()
            if path == "/items/MLB123456/sale_price?context=channel_marketplace":
                return self.sale(80, 100)
            raise AssertionError("Buscas de categoria nao devem acontecer: " + path)
        with patch.object(main.oauth_ml, "connected", return_value=True):
            with patch.object(main, "ml_api", side_effect=fake_api):
                result=main.register_affiliate_item("MLB123456", "https://meli.la/MeuLinkOficial")
        self.assertTrue(result["checked"])
        self.assertTrue(result["promotion_confirmed"])
        self.assertTrue(result["publicly_visible"])
        self.assertEqual(len(calls), 2)
        with main.db() as conn:
            row=conn.execute(
                "SELECT affiliate_url,is_promo,discount_percent,title FROM products WHERE id=?",
                ("MLB123456",),
            ).fetchone()
        self.assertEqual(row["affiliate_url"], "https://meli.la/MeuLinkOficial")
        self.assertEqual(row["is_promo"], 1)
        self.assertEqual(row["discount_percent"], 20.0)
        self.assertEqual(row["title"], "Fone de ouvido")

    def test_manual_link_saved_as_pending_when_item_access_is_denied(self):
        from urllib.error import HTTPError
        def fake_api(path):
            raise HTTPError("https://api.mercadolibre.com",403,"Forbidden",{},None)
        with patch.object(main.oauth_ml, "connected", return_value=True):
            with patch.object(main, "ml_api", side_effect=fake_api):
                result = main.register_affiliate_item("MLB777777", "https://mercadolivre.com/sec/abc123")
        self.assertFalse(result["checked"])
        self.assertFalse(result["publicly_visible"])
        self.assertIn("HTTP 403", result["status"])
        with main.db() as conn:
            row=conn.execute(
                "SELECT affiliate_url,is_promo,promo_checked_at,price FROM products WHERE id='MLB777777'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_promo"],0)
        self.assertIsNone(row["promo_checked_at"])
        self.assertIsNone(row["price"])

    def test_manual_link_without_discount_never_publishes(self):
        def fake_api(path):
            return self.sale(96,100) if "/sale_price" in path else self.item()
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",side_effect=fake_api):
                result=main.register_affiliate_item("MLB22222","https://meli.la/abc")
        self.assertTrue(result["checked"])
        self.assertFalse(result["promotion_confirmed"])
        with main.db() as conn:
            row=conn.execute("SELECT is_promo,promo_checked_at FROM products WHERE id='MLB22222'").fetchone()
        self.assertEqual(row["is_promo"],0)
        self.assertIsNotNone(row["promo_checked_at"])

    def test_manual_link_can_be_saved_before_oauth_but_is_not_published(self):
        with patch.object(main.oauth_ml,"connected",return_value=False):
            result=main.register_affiliate_item("MLB123455","https://meli.la/nome")
        self.assertFalse(result["checked"])
        self.assertFalse(result["publicly_visible"])
        with main.db() as conn:
            row=conn.execute("SELECT affiliate_url,is_promo FROM products WHERE id='MLB123455'").fetchone()
        self.assertEqual(row["is_promo"],0)
        self.assertEqual(row["affiliate_url"],"https://meli.la/nome")

    def test_manual_link_denies_bad_ids_and_untrusted_domains(self):
        cases=[
            ("MLBU22222","https://meli.la/ok"),
            ("MLB123456","https://meli.la.badsite.org/redirect"),
            ("MLB123456","http://meli.la/no"),
            ("MLB123456","https://example.org/fake"),
        ]
        for item_id, link in cases:
            with self.subTest(item_id=item_id,link=link):
                with self.assertRaises(ValueError):
                    main.register_affiliate_item(item_id,link)
        with main.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],0)

    def test_manual_revalidation_error_hides_previous_confirmed_promotion(self):
        from urllib.error import HTTPError
        with patch.object(main.oauth_ml,"connected",return_value=True):
            with patch.object(main,"ml_api",
                    side_effect=lambda path:self.sale() if "/sale_price" in path else self.item()):
                self.assertTrue(main.register_affiliate_item("MLB8888","https://meli.la/valid")["publicly_visible"])
            with patch.object(main,"ml_api",
                    side_effect=HTTPError("https://api.mercadolibre.com",403,"Forbidden",{},None)):
                self.assertFalse(main.register_affiliate_item("MLB8888","https://meli.la/another")["publicly_visible"])
        with main.db() as conn:
            row=conn.execute("SELECT is_promo,promo_checked_at,affiliate_url FROM products WHERE id='MLB8888'").fetchone()
        self.assertEqual(row["is_promo"],0)
        self.assertIsNone(row["promo_checked_at"])
        self.assertEqual(row["affiliate_url"],"https://meli.la/another")

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
