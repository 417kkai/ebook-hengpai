"""用自制短篇检验电子书转换，不依赖外部书籍。"""

import base64
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


CLI = Path(__file__).resolve().parents[1] / "ebook-hengpai/scripts/convert.py"
HTML = "{http://www.w3.org/1999/xhtml}"
OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6F5lP8AAAAASUVORK5CYII="
)


def make_epub(path, version="3.0", *, fixed=False, drm=False):
    epub3 = version == "3.0"
    nav_properties = ' properties="nav"' if epub3 else ""
    nav_type = ' epub:type="toc"' if epub3 else ""
    cover_properties = ' properties="cover-image"' if epub3 else ""
    progression = ' page-progression-direction="rtl"' if epub3 else ""
    modified = '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>' if epub3 else ""
    layout = '<meta property="rendition:layout">pre-paginated</meta>' if fixed else ""
    container = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    package = f"""<package xmlns="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="uid">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="uid">urn:example:hengpai-test</dc:identifier><dc:title>練習冊</dc:title>
        <dc:language>zh-TW</dc:language><dc:creator>測試作者</dc:creator>{modified}{layout}
      </metadata>
      <manifest>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"{nav_properties}/>
        <item id="one" href="Text/first%20page.xhtml" media-type="application/xhtml+xml"/>
        <item id="two" href="Text/second.xhtml" media-type="application/xhtml+xml"/>
        <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
        <item id="css" href="style.css" media-type="text/css"/>
        <item id="cover" href="Images/cover.png" media-type="image/png"{cover_properties}/>
      </manifest>
      <spine toc="ncx"{progression}><itemref idref="one"/><itemref idref="two"/></spine>
    </package>"""
    ncx = """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh-TW">
      <head><meta name="dtb:uid" content="urn:example:hengpai-test"/>
        <meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/>
        <meta name="dtb:maxPageNumber" content="0"/></head>
      <docTitle><text>練習冊</text></docTitle><navMap><navPoint id="n1" playOrder="1">
        <navLabel><text>第一節</text></navLabel><content src="Text/first%20page.xhtml#first"/>
      </navPoint></navMap></ncx>"""
    chapter = """<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-TW" lang="zh-TW" dir="rtl">
      <head><title>第一節</title><link rel="stylesheet" type="text/css" href="../style.css"/>
        <style type="text/css">.scene { -webkit-writing-mode: vertical-rl; }</style></head>
      <body><h1 id="first">第一節</h1><p class="scene" style="writing-mode: vertical-rl">「滑鼠」在電腦旁。</p>
        <p lang="en" xml:lang="en">An English sentence.</p>
        <p><img src="../Images/cover.png" alt="封面圖"/><a href="second.xhtml#later">續讀</a></p></body></html>"""
    second = """<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第二節</title></head>
      <body><h1 id="later">第二節</h1><p>她關掉電燈，翻開書。</p></body></html>"""
    if epub3:
        toc = f'<nav{nav_type}><h1>目錄</h1><ol><li><a href="Text/first%20page.xhtml#first">第一節</a></li></ol></nav>'
    else:
        toc = '<div><h1>目錄</h1><p><a href="Text/first%20page.xhtml#first">第一節</a></p></div>'
    nav = f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
       <head><title>目錄</title></head><body>{toc}</body></html>"""
    css = "html { writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; direction: rtl; }"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/toc.ncx", ncx)
        archive.writestr("OEBPS/nav.xhtml", nav)
        archive.writestr("OEBPS/Text/first page.xhtml", chapter)
        archive.writestr("OEBPS/Text/second.xhtml", second)
        archive.writestr("OEBPS/style.css", css)
        archive.writestr("OEBPS/Images/cover.png", PNG)
        if drm:
            archive.writestr("META-INF/encryption.xml", """<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">
                <EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
                <CipherData><CipherReference URI="OEBPS/Text/first%20page.xhtml"/></CipherData>
              </EncryptedData></encryption>""")


class ConversionTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="hengpai-test-")
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.source = self.workspace / "練習冊.epub"
        self.output = self.workspace / "完成.epub"

    def run_cli(self, *args, output=None):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(CLI), str(self.source), "-o",
             str(output or self.output), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
        )

    def assert_converts(self, version):
        make_epub(self.source, version)
        source_bytes = self.source.read_bytes()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        with ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            info = archive.infolist()[0]
            self.assertEqual((info.filename, info.compress_type), ("mimetype", ZIP_STORED))
            self.assertEqual(archive.read("OEBPS/Images/cover.png"), PNG)
            package = ET.fromstring(archive.read("OEBPS/content.opf"))
            spine = package.find(OPF + "spine")
            self.assertEqual(package.get("version"), version)
            self.assertEqual([e.get("idref") for e in spine], ["one", "two"])
            self.assertEqual(spine.get("page-progression-direction", "ltr"), "ltr")
            self.assertEqual(package.find(".//" + DC + "title").text, "练习册")
            self.assertRegex(package.find(".//" + DC + "language").text, r"^zh-(Hans|CN)")
            chapter = archive.read("OEBPS/Text/first page.xhtml").decode("utf-8")
            self.assertNotIn("vertical-rl", chapter)
            page = ET.fromstring(chapter)
            self.assertEqual(page.get("dir", "ltr"), "ltr")
            self.assertIn("“滑鼠”在电脑旁。", "".join(page.itertext()))
            self.assertEqual(page.find(f".//{HTML}p[@lang='en']").text, "An English sentence.")
            self.assertEqual(page.find(f".//{HTML}a").get("href"), "second.xhtml#later")
            self.assertEqual(page.find(f".//{HTML}img").get("alt"), "封面图")
            css = archive.read("OEBPS/style.css").decode("utf-8")
            self.assertNotRegex(css, re.compile(r"vertical-rl|direction\s*:\s*rtl"))
            nav = ET.fromstring(archive.read("OEBPS/nav.xhtml"))
            toc = ET.fromstring(archive.read("OEBPS/toc.ncx"))
            href = "Text/first%20page.xhtml#first"
            self.assertEqual(nav.find(f".//{HTML}a").get("href"), href)
            self.assertEqual(toc.find(f".//{NCX}content").get("src"), href)
            self.assertIn("第一节", "".join(toc.itertext()))
        self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_epub3(self):
        self.assert_converts("3.0")

    def test_epub2(self):
        self.assert_converts("2.0")

    def test_quote_and_terminology_switches(self):
        make_epub(self.source)
        result = self.run_cli("--keep-quotes", "--mainland-terms")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        with ZipFile(self.output) as archive:
            self.assertIn("「鼠标」在电脑旁。", archive.read("OEBPS/Text/first page.xhtml").decode("utf-8"))

    def test_existing_output_stays_intact(self):
        make_epub(self.source)
        self.output.write_bytes(b"original output")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"original output")
        self.assertNotEqual(self.run_cli(output=self.source).returncode, 0)

    def test_unsupported_source_does_not_create_output(self):
        for kind in ("fixed", "drm"):
            with self.subTest(kind=kind):
                make_epub(self.source, **{kind: True})
                self.assertNotEqual(self.run_cli().returncode, 0)
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
