"""Testes da vitrine V16 sem rede e sem credenciais de afiliado."""
from html.parser import HTMLParser
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "index.html").read_text(encoding="utf-8")


class AuditParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.references = []
        self.labels = []
        self.images_without_alt = []
        self.links = []
    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if data.get("id"):
            self.ids.append(data["id"])
        if tag == "a":
            self.links.append(data)
            if (data.get("href") or "").startswith("#"):
                self.references.append(data["href"][1:])
        if tag == "label" and data.get("for"):
            self.labels.append(data["for"])
        if tag == "img" and "alt" not in data:
            self.images_without_alt.append(data)


class HomeDesignTest(unittest.TestCase):
    def test_unique_ids_and_valid_navigation_targets(self):
        doc = AuditParser()
        doc.feed(HTML)
        self.assertEqual(len(doc.ids), len(set(doc.ids)))
        for anchor in doc.references:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, doc.ids)
        for input_id in ("search", "merchant", "category", "sort"):
            self.assertIn(input_id, doc.labels)
            self.assertIn(input_id, doc.ids)
        self.assertEqual(doc.images_without_alt, [])

    def test_v16_features_present_without_fake_offers(self):
        for feature in (
            'id="search"', 'id="categoryGrid"', 'id="featuredCards"',
            'id="partnerGrid"', 'id="cards"', 'id="siteToast"',
            'id="transparencia"', 'class="bag-illustration"',
            'id="how-title"', 'id="filterFeedback"', "Compartilhar oferta",
            'rel="sponsored noopener noreferrer"',
        ):
            with self.subTest(feature=feature):
                self.assertIn(feature, HTML)
        self.assertIn('fetch("/api/products",{cache:"no-store"})', HTML)
        self.assertNotIn("R$ 999,90", HTML)
        self.assertNotIn('4.9 de 5', HTML)

    @unittest.skipUnless(shutil.which("node"), "Node.js nao instalado")
    def test_browser_product_safety_without_real_network(self):
        """Executa a logica de filtro V16 em um contexto JavaScript isolado."""
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const found=html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(found,"script principal ausente");
const js=found[1].replace(/\nload\(\);\s*$/, '\n');
const mock={
 window:{location:{search:''}},URL,URLSearchParams,
 document:{getElementById:()=>({addEventListener:()=>{}})},
 navigator:{}
};
vm.createContext(mock);
vm.runInContext(js+'\nthis.exports={canDisplay,safeAffiliateURL,normalize};',mock);
const api=mock.exports;
const valid={title:"Notebook com promoção",id:"product1",price:900,
 original_price:1000,discount_percent:10,
 affiliate_url:"https://www.awin1.com/cread.php?x=3"};
assert.strictEqual(Boolean(api.canDisplay(valid)),true);
for(const invalid of [
  {...valid,discount_percent:95},
  {...valid,discount_percent:0},
  {...valid,price:1000},
  {...valid,affiliate_url:"javascript:alert(1)"},
  {...valid,affiliate_url:"http://www.awin1.com/cread.php"},
  {...valid,affiliate_url:"https://user:pass@www.awin1.com/x"},
]){
 assert.strictEqual(Boolean(api.canDisplay(invalid)),false);
}
assert.strictEqual(api.normalize("Câmera e Áudio"),"camera e audio");
console.log("Frontend validation OK");
"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "audit.js"
            path.write_text(code, encoding="utf-8")
            result = subprocess.run(
                ["node", str(path), str(BASE / "index.html")],
                capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
