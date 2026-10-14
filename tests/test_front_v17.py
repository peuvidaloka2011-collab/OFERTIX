"""V17: somente anúncios com desconto comprovável aparecem na vitrine."""
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
        super().__init__()
        self.ids=[]
        self.fragments=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if "id" in a:self.ids.append(a["id"])
        if tag=="a" and a.get("href","").startswith("#"):
            self.fragments.append(a["href"][1:])


class OnlyPromotionFrontTests(unittest.TestCase):
    def test_show_only_promotions_and_valid_navigation(self):
        doc=AuditHTML();doc.feed(HTML)
        self.assertEqual(len(doc.ids),len(set(doc.ids)))
        for fragment in doc.fragments:
            self.assertIn(fragment,doc.ids)
        self.assertIn("featuredCards",doc.ids)
        self.assertIn("cards",doc.ids)
        self.assertIn("partnerGrid",doc.ids)
        self.assertNotIn("catalogo-parceiros",doc.ids)
        self.assertNotIn("partnerCatalogCards",doc.ids)
        self.assertNotIn("partnerCatalogCount",doc.ids)
        self.assertNotIn("/api/partner-products",HTML)
        self.assertNotIn("makeRegularCard",HTML)

    @unittest.skipUnless(shutil.which("node"),"Node.js ausente")
    def test_embedded_javascript_compiles(self):
        match=re.search(r"<script>(.*?)</script>",HTML,re.S)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"home.js"
            path.write_text(match.group(1),encoding="utf-8")
            result=subprocess.run(["node","--check",str(path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=="__main__":
    unittest.main()
