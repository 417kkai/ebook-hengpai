"""Convert a reflowable EPUB without rebuilding its reading order or assets."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import html.entities
import posixpath
import re
import unicodedata

from lxml import etree
from opencc import OpenCC
import tinycss2


OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
HTML = "{http://www.w3.org/1999/xhtml}"
SVG = "{http://www.w3.org/2000/svg}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OCF = "{urn:oasis:names:tc:opendocument:xmlns:container}"
ENC = "{http://www.w3.org/2001/04/xmlenc#}"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
LANGUAGE = "zh-Hans-CN"
MIMETYPE = b"application/epub+zip"

FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}
DOCUMENT_TYPES = {"application/xhtml+xml", "image/svg+xml", "application/x-dtbncx+xml"}
WRITING_MODES = {
    "writing-mode", "-epub-writing-mode", "-webkit-writing-mode", "-ms-writing-mode",
    "-moz-writing-mode", "-o-writing-mode",
}
VERTICAL_PROPERTIES = {
    "text-orientation", "-epub-text-orientation", "-webkit-text-orientation",
    "text-combine-upright", "-webkit-text-combine", "-epub-text-combine",
    "text-combine-horizontal", "-webkit-text-combine-horizontal",
    "-epub-text-combine-horizontal", "glyph-orientation-vertical",
}
LAYOUT_PROPERTIES = WRITING_MODES | VERTICAL_PROPERTIES | {"direction"}

QUOTES = str.maketrans("「」『』", "“”‘’")
VERTICAL_FORMS = {}
for code in range(0xFE10, 0xFE50):
    decomposition = unicodedata.decomposition(chr(code)).split()
    if len(decomposition) == 2 and decomposition[0] == "<vertical>":
        VERTICAL_FORMS[code] = int(decomposition[1], 16)

UNTRANSLATED_XML = {
    HTML + "script", HTML + "style", SVG + "script", SVG + "style", SVG + "metadata",
}
HUMAN_ATTRIBUTES = {"alt", "title", "aria-label", "aria-description", "summary"}
DC_TEXT = {
    DC + name for name in (
        "title", "creator", "subject", "description", "publisher", "contributor",
        "coverage", "rights",
    )
}
OPF_TEXT_PROPERTIES = {"alternate-script", "belongs-to-collection", "file-as"}
OPF_TEXT_NAMES = {"calibre:series", "calibre:title_sort"}
XML_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]+);")
XML_PROTECTED = re.compile(rb"(<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>)")
XML_BUILTINS = {b"amp", b"lt", b"gt", b"apos", b"quot"}


class EpubError(ValueError):
    """This EPUB cannot be transformed with the available input resources."""


def _entity_to_numeric(match: re.Match[bytes]) -> bytes:
    key = match.group(1)
    if key in XML_BUILTINS:
        return match.group(0)
    replacement = html.entities.html5.get((key + b";").decode("ascii"))
    if replacement is None:
        return match.group(0)
    return "".join(f"&#{ord(character)};" for character in replacement).encode("ascii")


def _parse_xml(data: bytes, name: str) -> etree._Element:
    def parse(body: bytes) -> etree._Element:
        return etree.fromstring(
            body,
            parser=etree.XMLParser(no_network=True, resolve_entities=False, load_dtd=False),
        )

    try:
        return parse(data)
    except etree.XMLSyntaxError as original:
        # Some older EPUBs use HTML entity names without declaring an entity table.
        if "Entity" in str(original) and "not defined" in str(original):
            chunks = XML_PROTECTED.split(data)
            repaired = b"".join(
                chunk if index % 2 else XML_ENTITY.sub(_entity_to_numeric, chunk)
                for index, chunk in enumerate(chunks)
            )
            if repaired != data:
                try:
                    return parse(repaired)
                except etree.XMLSyntaxError:
                    pass
        raise EpubError(f"XML 格式错误 {name}: {original}") from original


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)


def _normalize_language(value: str) -> str:
    primary = value.lower().replace("_", "-").split("-")[0]
    if primary in {"zh", "zho", "chi", "cmn"}:
        return LANGUAGE
    return value


def _local_path(base: str, href: str) -> str | None:
    url = urlsplit(href)
    if url.scheme or url.netloc:
        return None
    path = unquote(url.path)
    full = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if not path or full in {".", ".."} or full.startswith(("/", "../")) or "\\" in full:
        raise EpubError(f"无效的 EPUB 资源路径 {href!r}")
    return full


def _archive_files(archive: ZipFile) -> set[str]:
    files: set[str] = set()
    for entry in archive.infolist():
        name = entry.filename
        if name in files or "\\" in name or ":" in name or any(
            part in {"", ".", ".."} for part in name.rstrip("/").split("/")
        ):
            raise EpubError(f"EPUB 包含重复或无效的存档路径 {name!r}")
        if entry.flag_bits & 1:
            raise EpubError("EPUB 存档已加密")
        files.add(name)
    return files


def _check_encryption(archive: ZipFile, files: set[str]) -> None:
    name = "META-INF/encryption.xml"
    if name not in files:
        return
    root = _parse_xml(archive.read(name), name)
    for record in root.iter(ENC + "EncryptedData"):
        encryption = record.find(ENC + "EncryptionMethod")
        if encryption is None or encryption.get("Algorithm") not in FONT_OBFUSCATION:
            raise EpubError("EPUB 含加密内容，请使用无 DRM 版本")


def _check_layout(archive: ZipFile, package: etree._Element, files: set[str]) -> None:
    metadata = package.find(OPF + "metadata")
    for meta in metadata.findall(OPF + "meta"):
        if (
            meta.get("property") == "rendition:layout" and (meta.text or "").strip() == "pre-paginated"
        ) or (
            meta.get("name") in {"fixed-layout", "book-type"}
            and meta.get("content", "").lower() in {"true", "fixed-layout", "comic"}
        ):
            raise EpubError("固定版式 EPUB 需要人工重排，无法直接转为横排")
    for item in package.findall("./" + OPF + "spine/" + OPF + "itemref"):
        if "rendition:layout-pre-paginated" in item.get("properties", "").split():
            raise EpubError("EPUB 含固定版式页，无法直接转为横排")
    for name in (
        "META-INF/com.apple.ibooks.display-options.xml",
        "META-INF/com.kobobooks.display-options.xml",
    ):
        if name not in files:
            continue
        root = _parse_xml(archive.read(name), name)
        if any(
            option.get("name") == "fixed-layout" and (option.text or "").strip().lower() == "true"
            for option in root.iter()
        ):
            raise EpubError("固定版式 EPUB 需要人工重排，无法直接转为横排")


class _Conversion:
    def __init__(self, mainland_quotes: bool, mainland_terms: bool):
        config = "tw2sp" if mainland_terms else "tw2s"
        try:
            self.converter = OpenCC(config + ".json")
        except (FileNotFoundError, RuntimeError):
            # The pure-Python OpenCC port expects configuration names without .json.
            self.converter = OpenCC(config)
        self.mainland_quotes = mainland_quotes
        self.counts: Counter[str] = Counter()

    def text(self, original: str | None) -> str | None:
        if not original or not original.strip():
            return original
        result = self.converter.convert(original.translate(VERTICAL_FORMS))
        if self.mainland_quotes:
            result = result.translate(QUOTES)
        self.counts["converted_text_nodes"] += result != original
        return result

    def css_nodes(self, nodes: list) -> tuple[list, bool]:
        if any(node.type == "error" for node in nodes):
            # An unknown CSS extension may be valid for its original reader.
            return nodes, False
        result = []
        changed = False
        for node in nodes:
            if node.type == "declaration" and node.lower_name in LAYOUT_PROPERTIES:
                if node.lower_name in WRITING_MODES:
                    value = "lr-tb" if node.lower_name == "-ms-writing-mode" else "horizontal-tb"
                    if tinycss2.serialize(node.value).strip().lower() == value:
                        result.append(node)
                        continue
                    node.value = tinycss2.parse_component_value_list(" " + value)
                    result.append(node)
                self.counts["layout_rules_updated"] += 1
                changed = True
                continue
            if node.type in {"qualified-rule", "at-rule"} and node.content is not None:
                members = tinycss2.parse_blocks_contents(node.content)
                rewritten, inner_changed = self.css_nodes(members)
                if inner_changed:
                    node.content = tinycss2.parse_component_value_list(tinycss2.serialize(rewritten))
                    changed = True
            result.append(node)
        return result, changed

    def css_text(self, text: str, inline: bool = False) -> str:
        parse = tinycss2.parse_declaration_list if inline else tinycss2.parse_stylesheet
        updated, changed = self.css_nodes(parse(text))
        return tinycss2.serialize(updated) if changed else text

    def css_bytes(self, data: bytes) -> bytes:
        rules, encoding = tinycss2.parse_stylesheet_bytes(data)
        updated, changed = self.css_nodes(rules)
        return tinycss2.serialize(updated).encode(encoding.codec_info.name) if changed else data

    def xhtml(self, root: etree._Element, version: str) -> None:
        if root.tag == HTML + "html":
            language = _normalize_language(root.get(XML_LANG) or root.get("lang") or LANGUAGE)
            root.set(XML_LANG, language)
            if version.startswith("3") or "lang" in root.attrib:
                root.set("lang", language)
            root.set("dir", "ltr")
        self.content(root)
        if root.tag == HTML + "html":
            head = root.find(HTML + "head")
            fallback = "html { writing-mode: horizontal-tb; }"
            if head is not None and not any(
                (style.text or "").strip() == fallback for style in head.findall(HTML + "style")
            ):
                etree.SubElement(head, HTML + "style", type="text/css").text = fallback

    def content(self, node: etree._Element, inherited_language: str | None = None) -> None:
        if not isinstance(node.tag, str):
            return
        for lang_key in (XML_LANG, "lang"):
            if lang_key in node.attrib:
                node.set(lang_key, _normalize_language(node.get(lang_key)))
        language = node.get(XML_LANG) or node.get("lang") or inherited_language
        if node.tag.startswith(HTML) and language == LANGUAGE and node.get("dir") == "rtl":
            node.set("dir", "ltr")
        if "style" in node.attrib:
            node.set("style", self.css_text(node.get("style"), inline=True))
        if node.tag.startswith(SVG) and "writing-mode" in node.attrib:
            if node.get("writing-mode") not in {"lr", "lr-tb", "horizontal-tb"}:
                node.set("writing-mode", "lr-tb")
                self.counts["layout_rules_updated"] += 1
        for key in HUMAN_ATTRIBUTES.intersection(node.attrib):
            node.set(key, self.text(node.get(key)))
        if node.tag in {HTML + "style", SVG + "style"}:
            if node.text:
                node.text = self.css_text(node.text)
            return
        if node.tag in UNTRANSLATED_XML:
            return
        node.text = self.text(node.text)
        for child in node:
            self.content(child, language)
            child.tail = self.text(child.tail)

    def package(self, package: etree._Element, spine: etree._Element, version: str) -> None:
        metadata = package.find(OPF + "metadata")
        for node in metadata:
            if node.tag in DC_TEXT:
                node.text = self.text(node.text)
                sort_name = OPF + "file-as"
                if sort_name in node.attrib:
                    node.set(sort_name, self.text(node.get(sort_name)))
            elif node.tag == DC + "language" and node.text:
                node.text = _normalize_language(node.text.strip())
            elif node.tag == OPF + "meta":
                if node.get("property") in OPF_TEXT_PROPERTIES:
                    # Alternate-script is often a catalog transliteration or original form.
                    language = node.get(XML_LANG)
                    if node.get("property") != "alternate-script" or language and _normalize_language(language) == LANGUAGE:
                        node.text = self.text(node.text)
                        if language:
                            node.set(XML_LANG, _normalize_language(language))
                if node.get("name") in OPF_TEXT_NAMES and "content" in node.attrib:
                    node.set("content", self.text(node.get("content")))
        if version.startswith("3"):
            package.set(XML_LANG, _normalize_language(package.get(XML_LANG) or LANGUAGE))
            package.set("dir", "ltr")
            spine.set("page-progression-direction", "ltr")
        else:
            # EPUB 2 uses the default LTR progression and has no spine direction attribute.
            spine.attrib.pop("page-progression-direction", None)
        for ref in package.findall("./" + OPF + "guide/" + OPF + "reference"):
            if "title" in ref.attrib:
                ref.set("title", self.text(ref.get("title")))


def _manifest(archive: ZipFile, files: set[str]) -> tuple[str, etree._Element, dict[str, str]]:
    container_name = "META-INF/container.xml"
    if container_name not in files or "mimetype" not in files:
        raise EpubError("缺少 EPUB 必需的 mimetype 或 container.xml")
    if archive.read("mimetype").strip() != MIMETYPE:
        raise EpubError("文件的 mimetype 不是 EPUB")
    container = _parse_xml(archive.read(container_name), container_name)
    packages = container.findall("./" + OCF + "rootfiles/" + OCF + "rootfile")
    if len(packages) != 1:
        raise EpubError("此转换器只支持单一排版版本的 EPUB")
    opf_name = packages[0].get("full-path", "")
    if opf_name not in files:
        opf_name = unquote(opf_name)
    if opf_name not in files:
        raise EpubError("找不到 EPUB 书籍包描述文件")
    package = _parse_xml(archive.read(opf_name), opf_name)
    manifest = package.find(OPF + "manifest")
    if package.tag != OPF + "package" or manifest is None or package.find(OPF + "metadata") is None:
        raise EpubError("EPUB 包描述文件无效")
    resources = {}
    for item in manifest.findall(OPF + "item"):
        path = _local_path(opf_name, item.get("href", ""))
        if path is None:
            continue
        if path not in files:
            raise EpubError(f"EPUB 缺少引用资源 {path!r}")
        resources[path] = item.get("media-type", "")
    return opf_name, package, resources


def _visible_text(node: etree._Element):
    if (
        not isinstance(node.tag, str)
        or node.get("hidden") is not None
        or node.tag == HTML + "nav"
        or node.tag in UNTRANSLATED_XML
    ):
        return
    if node.text:
        yield node.text
    for child in node:
        yield from _visible_text(child)
        if child.tail:
            yield child.tail


def _export(
    archive: ZipFile,
    destination: ZipFile,
    conversion: _Conversion,
    opf_name: str,
    package: etree._Element,
    resources: dict[str, str],
    version: str,
) -> None:
    destination.writestr("mimetype", MIMETYPE, compress_type=ZIP_STORED)
    for entry in archive.infolist():
        name = entry.filename
        media = resources.get(name)
        if name == "mimetype":
            continue
        if name == opf_name:
            destination.writestr(name, _xml_bytes(package))
        elif media == "text/css":
            destination.writestr(name, conversion.css_bytes(archive.read(entry)))
        elif media in DOCUMENT_TYPES:
            root = _parse_xml(archive.read(entry), name)
            if media == "application/xhtml+xml":
                conversion.xhtml(root, version)
                body = root.find(HTML + "body")
                if body is not None:
                    conversion.counts["body_content"] += any(
                        character.isalnum()
                        for text in _visible_text(body)
                        for character in text
                    )
            else:
                conversion.content(root)
            if media != "application/x-dtbncx+xml":
                conversion.counts["content_documents"] += 1
            destination.writestr(name, _xml_bytes(root))
        elif entry.is_dir():
            destination.writestr(name, b"")
        else:
            with archive.open(entry) as resource, destination.open(name, "w") as copy:
                copyfileobj(resource, copy, length=1024 * 1024)


def convert_epub(
    source: Path,
    output: Path,
    *,
    mainland_quotes: bool = True,
    mainland_terms: bool = False,
) -> dict:
    """Replace a caller-owned staging path with the converted EPUB and return its report."""
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve() or output.exists() and source.samefile(output):
        raise EpubError("输出路径与原文件重合")
    conversion = _Conversion(mainland_quotes, mainland_terms)
    with ZipFile(source) as archive:
        files = _archive_files(archive)
        _check_encryption(archive, files)
        opf_name, package, resources = _manifest(archive, files)
        version = package.get("version", "")
        if not version.startswith(("2.", "3.")):
            raise EpubError(f"不支持的 EPUB 版本 {version!r}")
        spine = package.find(OPF + "spine")
        if spine is None or not len(spine):
            raise EpubError("EPUB 包缺少阅读顺序")
        _check_layout(archive, package, files)
        conversion.package(package, spine, version)

        with NamedTemporaryFile(dir=output.parent, suffix=".epub.tmp", delete=False) as draft:
            draft_path = Path(draft.name)
        try:
            with ZipFile(draft_path, "w", compression=ZIP_DEFLATED) as destination:
                _export(archive, destination, conversion, opf_name, package, resources, version)
            with ZipFile(draft_path) as written:
                invalid = written.testzip()
                if invalid is not None:
                    raise EpubError(f"输出 EPUB 校验失败 {invalid!r}")
                if set(written.namelist()) != files:
                    raise EpubError("输出 EPUB 资源清单与原文件不一致")
            draft_path.replace(output)
        finally:
            draft_path.unlink(missing_ok=True)

    warnings = []
    if not conversion.counts["body_content"]:
        warnings.append("未检测到 XHTML 正文文字，图片内的文字没有转换")
    return {
        "epub_version": version,
        "content_documents": conversion.counts["content_documents"],
        "spine_items": len(spine),
        "images": sum(media.startswith("image/") for media in resources.values()),
        "converted_text_nodes": conversion.counts["converted_text_nodes"],
        "layout_rules_updated": conversion.counts["layout_rules_updated"],
        "warnings": warnings,
    }
