"""Verificacoes offline da nova seção de catálogo Carraro."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

HTML=(Path(__file__).resolve().parents[1]/"index.html").read_text(encoding="utf-8")


class AuditHTML(HTMLParser):
    def __init__(self):
        super().__init__();self.ids=[];self.fragments=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if "id" in a:self.ids.append(a["id"])
        if tag=="a" and a.get("href","").startswith("#"):
            self.fragments.append(a["href"][1:])


class PartnerCatalogFrontTests(unittest.TestCase):
    def test_catalog_section_is_distinct_from_promotions(self):
        doc=AuditHTML();doc.feed(HTML)
        self.assertEqual(len(doc.ids),len(set(doc.ids)))
        for fragment in doc.fragments:
            self.assertIn(fragment,doc.ids)
        self.assertIn("catalogo-parceiros",doc.ids)
        self.assertIn("partnerCatalogCount",doc.ids)
        self.assertIn("partnerCatalogCards",doc.ids)
        self.assertIn("featuredCards",doc.ids)
        self.assertIn("Desconto não confirmado",HTML)
        self.assertIn("/api/partner-products",HTML)
        self.assertIn("makeRegularCard",HTML)

    @unittest.skipUnless(shutil.which("node"),"Node.js ausente")
    def test_embedded_javascript_compiles(self):
        match=re.search(r"<script>(.*?)</script>",HTML,re.S)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"home.js"
            path.write_text(match.group(1),encoding="utf-8")
            result=subprocess.run(["node","--check",str(path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=="__main__":unittest.main()
