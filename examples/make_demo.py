"""Build the original Traditional Chinese sample using only Python's standard library."""

import argparse
from pathlib import Path
import struct
import zipfile
import zlib


TITLE = "等一盞港燈"
OUTPUT_NAME = "demo-zh-tw.epub"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="EPUB/book.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

PACKAGE = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         unique-identifier="book-id" xml:lang="zh-TW">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:51bbf7a5-cda4-4bbe-87bd-a9b983b4c750</dc:identifier>
    <dc:title>{TITLE}</dc:title>
    <dc:language>zh-TW</dc:language>
    <meta property="dcterms:modified">2026-11-27T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="part-1" href="part-1.xhtml" media-type="application/xhtml+xml"/>
    <item id="part-2" href="part-2.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="book.css" media-type="text/css"/>
    <item id="lamp" href="harbor.png" media-type="image/png"/>
  </manifest>
  <spine page-progression-direction="rtl">
    <itemref idref="part-1"/>
    <itemref idref="part-2"/>
  </spine>
</package>
"""

STYLE = """html {
  writing-mode: vertical-rl;
  text-orientation: mixed;
  line-break: strict;
}
body {
  margin: 1.5em;
  font-family: "Noto Serif CJK TC", "Songti TC", "PMingLiU", serif;
  line-height: 1.85;
}
h1, h2 { line-height: 1.4; }
p { text-indent: 2em; }
figure { margin: 1em; }
figure img { width: 10em; height: auto; display: block; }
figcaption { font-size: 0.85em; }
"""

SECTIONS = (
    (
        "末班渡輪",
        f"""<h1>{TITLE}</h1>
  <h2 id="ferry">末班渡輪</h2>
  <p>天黑後，雨剛停。阿澄從旗津搭末班渡輪回來，帆布袋裡揣著外婆修好的收音機。船引擎一響，外婆貼在機殼上的紙條就跟著抖，上頭寫著：「轉到海風聽得見的地方。」</p>
  <p>她在碼頭邊試了幾次。先是沙沙的雜音，接著有人報明天的潮水。再往右一點，她聽見有人哼著一支陌生的小調，一句沒哼完，船便靠了岸。</p>
  <p>她想起外婆交代過：「回去的時候，幫我看看老碼頭那盞燈。」</p>
  <figure>
    <img src="harbor.png" alt="深藍色的港面上，棧橋盡頭亮著一盞黃燈"/>
    <figcaption>雨後的老碼頭</figcaption>
  </figure>
  <p><a href="part-2.xhtml#lamp">下一節　有人等船</a></p>""",
    ),
    (
        "有人等船",
        """<h1 id="lamp">有人等船</h1>
  <p>老碼頭已經不靠船了，棧橋入口還掛著一把生鏽的鎖。賣黑輪的阿伯正在收攤，見阿澄站在那裡，便從圍裙口袋掏出一串鑰匙。</p>
  <p>「妳外婆以前在這裡等妳外公收工。」他挑出一支短鑰匙，「她嫌港邊暗，借了我一盞燈。後來船不來了，她還是每天來開。」</p>
  <p>阿澄把燈打開，拍了張照片傳給外婆。等紅燈時，手機響了一聲。外婆只回了四個字：「有看到喔。」</p>
  <p><a href="part-1.xhtml#ferry">回到第一節</a></p>""",
    ),
)


def xhtml(title, body, stylesheet=False):
    style_link = '<link rel="stylesheet" href="book.css"/>' if stylesheet else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-TW" xml:lang="zh-TW" dir="ltr">
<head><title>{title}</title>{style_link}</head>
<body>
  {body}
</body>
</html>
"""


def harbor_png():
    """Draw a small geometric harbor scene and encode it as an indexed PNG."""
    width, height = 192, 112
    palette = bytes.fromhex("75888e 324f60 4a6b7a 263541 d9a44b f8d581")
    pixels = bytearray()
    for y in range(height):
        row = bytearray(width)
        for x in range(width):
            color = 0 if y < 60 else 1
            if y >= 63 and y % 7 == 0 and (x + 13 * y) % 38 < 16:
                color = 2
            if 80 < y < 106 and abs(x - 134) < (y - 76) // 7 and y % 4 < 2:
                color = 4
            if (70 <= y < 76 and x <= 145) or (76 <= y < 91 and 81 <= x < 85):
                color = 3
            if 47 <= y < 72 and 132 <= x < 136:
                color = 3
            if 38 <= y < 47 and 128 <= x < 140:
                color = 5 if 39 <= y < 46 and 130 <= x < 138 else 3
            row[x] = color
        pixels.extend(b"\0" + row)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
        + chunk(b"PLTE", palette)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def build_demo(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / OUTPUT_NAME
    navigation = """<nav epub:type="toc" id="toc">
    <h1>目錄</h1>
    <ol>
      <li><a href="part-1.xhtml#ferry">末班渡輪</a></li>
      <li><a href="part-2.xhtml#lamp">有人等船</a></li>
    </ol>
  </nav>"""
    resources = {
        "META-INF/container.xml": CONTAINER,
        "EPUB/book.opf": PACKAGE,
        "EPUB/book.css": STYLE,
        "EPUB/nav.xhtml": xhtml("目錄", navigation),
    }
    for number, (title, body) in enumerate(SECTIONS, start=1):
        resources[f"EPUB/part-{number}.xhtml"] = xhtml(title, body, stylesheet=True)
    resources["EPUB/harbor.png"] = harbor_png()

    with zipfile.ZipFile(destination, "w") as book:
        book.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip")
        for name, payload in resources.items():
            book.writestr(zipfile.ZipInfo(name), payload, compress_type=zipfile.ZIP_DEFLATED)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="directory for the generated EPUB")
    args = parser.parse_args()
    print(build_demo(args.output_dir))


if __name__ == "__main__":
    main()
