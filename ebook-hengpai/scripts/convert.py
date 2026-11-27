"""在本地将电子书转为简体中文横排。"""

import argparse
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZipFile

FORMATS = {".epub", ".azw3", ".mobi", ".pdf"}
PDF_NOTICE = "PDF 输入按文字层重排，请抽查竖排栏序、换段、页眉页脚和缺字。"
NO_PDF_TEXT = "未从 PDF 提取到可重排的正文。请使用有文字层的版本，或先进行 OCR 并校对。"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="源文件，支持 EPUB、AZW3、MOBI、PDF")
    parser.add_argument("-o", "--output", type=Path, help="输出文件，扩展名决定格式，默认为 EPUB")
    parser.add_argument("--keep-quotes", action="store_true", help="保留原书的角引号")
    parser.add_argument("--mainland-terms", action="store_true", help="同时转换 OpenCC 收录的两岸惯用词")
    parser.add_argument("--calibre", type=Path, metavar="PATH", help="Calibre ebook-convert 的可执行文件路径")
    parser.add_argument("--pdf-font", metavar="FAMILY", help="PDF 输出使用的已安装字体族名")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出转换报告")
    return parser.parse_args(argv)


def calibre_executable(override):
    if override is not None:
        executable = override.expanduser().absolute()
        if not executable.is_file():
            raise ValueError(f"找不到指定的 ebook-convert：{executable}")
        return str(executable)

    executable = shutil.which("ebook-convert")
    if executable:
        return executable

    candidates = [Path("/Applications/calibre.app/Contents/MacOS/ebook-convert")]
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if folder := os.environ.get(variable):
            candidates.append(Path(folder) / "Calibre2" / "ebook-convert.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ValueError("此格式需要 Calibre。请安装 Calibre，或用 --calibre 指定 ebook-convert 的路径。")


def run_calibre(executable, source, output, stage, *options):
    environment = os.environ.copy()
    # Use built-in conversion plugins and keep Calibre from changing user preferences.
    for key, name in (
        ("CALIBRE_CONFIG_DIRECTORY", "calibre-config"),
        ("CALIBRE_CACHE_DIRECTORY", "calibre-cache"),
        ("CALIBRE_TEMP_DIR", "calibre-temp"),
    ):
        directory = stage / name
        directory.mkdir(exist_ok=True)
        environment[key] = str(directory)

    result = subprocess.run(
        [executable, str(source), str(output), *options],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if re.search(r"\bDRM\b|encrypted|password|加密|密码", result.stdout, re.IGNORECASE):
            raise ValueError("Calibre 无法读取受 DRM 或密码保护的文件。请使用可直接读取的副本。")
        if source.suffix.lower() == ".pdf" and re.search(
            r"no text|without text|image.only|scanned|没有文字", result.stdout, re.IGNORECASE
        ):
            raise ValueError(NO_PDF_TEXT)
        detail = "\n".join(lines[-4:])
        raise ValueError(f"Calibre 转换失败（退出码 {result.returncode}）。\n{detail}".rstrip())
    verify_signature(output)


def verify_signature(path):
    if not path.is_file():
        raise ValueError(f"转换程序没有生成文件：{path.name}")
    with path.open("rb") as stream:
        header = stream.read(80)
    extension = path.suffix.lower()
    valid = (
        (extension == ".pdf" and header.startswith(b"%PDF-"))
        or (extension in {".azw3", ".mobi"} and header[60:68] == b"BOOKMOBI")
        or (extension == ".epub" and header.startswith(b"PK\x03\x04"))
    )
    if not valid:
        raise ValueError(f"输出文件的格式与扩展名不符：{path.name}")


def has_pdf_text(epub):
    from epub_core import EpubError, HTML, OPF, SVG, _archive_files, _local_path, _manifest, _parse_xml

    with ZipFile(epub) as archive:
        package_path, package, resources = _manifest(archive, _archive_files(archive))
        items = {item.get("id"): item for item in package.findall(f"./{OPF}manifest/{OPF}item")}
        front_matter = {
            _local_path(package_path, node.get("href", ""))
            for node in package.findall(f"./{OPF}guide/{OPF}reference")
            if node.get("type") in {"cover", "toc"}
        }
        for ref in package.findall(f"./{OPF}spine/{OPF}itemref"):
            item = items.get(ref.get("idref"))
            if item is None:
                raise EpubError("Calibre 生成的 EPUB 含无效阅读顺序。")
            path = _local_path(package_path, item.get("href", ""))
            if (
                path is None
                or resources.get(path) != "application/xhtml+xml"
                or path in front_matter
                or ref.get("linear") == "no"
                or "nav" in item.get("properties", "").split()
                or Path(path).stem.lower() in {"cover", "titlepage", "toc", "nav"}
            ):
                continue
            chapter = _parse_xml(archive.read(path), path)
            body = chapter.find(HTML + "body")
            if body is not None and any(
                any(character.isalpha() for character in text)
                for text in body.xpath(
                    ".//text()[not(ancestor::h:script or ancestor::h:style or ancestor::h:nav "
                    "or ancestor::s:svg or ancestor::*[@hidden])]",
                    namespaces={"h": HTML[1:-1], "s": SVG[1:-1]},
                )
            ):
                return True
    return False


def publish(staged, output):
    # Windows rename, unlike POSIX rename, never replaces an existing target.
    if os.name == "nt":
        staged.rename(output)
        return

    # A hard link exposes the completed file atomically and fails if the name is taken.
    try:
        os.link(staged, output)
        return
    except OSError as error:
        if error.errno not in {errno.EPERM, errno.EACCES, errno.EXDEV, errno.ENOTSUP, errno.ENOSYS}:
            raise

    # Some removable or network filesystems lack hard links. Reserve the name exclusively.
    reserved = False
    try:
        with staged.open("rb") as source, output.open("xb") as target:
            reserved = True
            shutil.copyfileobj(source, target)
    except BaseException:
        if reserved:
            output.unlink(missing_ok=True)
        raise


def convert(args):
    from epub_core import convert_epub

    source = args.input.expanduser().absolute()
    output = (args.output or source.with_name(f"{source.stem}（简体横排）.epub")).expanduser().absolute()
    source_format = source.suffix.lower()
    destination_format = output.suffix.lower()
    if not source.is_file():
        raise ValueError(f"找不到源文件：{source}")
    if source_format not in FORMATS or destination_format not in FORMATS:
        raise ValueError("输入和输出格式限于 EPUB、AZW3、MOBI、PDF，请检查文件扩展名。")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"输出路径已存在，请用 -o 指定新文件：{output}")
    if not output.parent.is_dir():
        raise ValueError(f"输出目录不存在：{output.parent}")
    if args.pdf_font and destination_format != ".pdf":
        raise ValueError("--pdf-font 仅用于 PDF 输出。")

    uses_calibre = (source_format, destination_format) != (".epub", ".epub")
    executable = calibre_executable(args.calibre) if uses_calibre else None
    with tempfile.TemporaryDirectory(prefix=".ebook-hengpai-", dir=output.parent) as temporary:
        stage = Path(temporary)
        reading_epub = source
        if source_format != ".epub":
            reading_epub = stage / "source.epub"
            run_calibre(executable, source, reading_epub, stage, "--no-default-epub-cover")
            if source_format == ".pdf" and not has_pdf_text(reading_epub):
                raise ValueError(NO_PDF_TEXT)

        result_epub = stage / "horizontal.epub"
        report = convert_epub(
            reading_epub,
            result_epub,
            mainland_quotes=not args.keep_quotes,
            mainland_terms=args.mainland_terms,
        )
        verify_signature(result_epub)
        deliverable = result_epub
        if destination_format != ".epub":
            deliverable = stage / f"horizontal{destination_format}"
            options = []
            if args.pdf_font:
                options.extend(["--pdf-serif-family", args.pdf_font, "--pdf-sans-family", args.pdf_font])
            run_calibre(executable, result_epub, deliverable, stage, *options)
        try:
            publish(deliverable, output)
        except FileExistsError as error:
            raise FileExistsError(f"输出路径已存在，请用 -o 指定新文件：{output}") from error

    warnings = list(report.get("warnings", []))
    if source_format == ".pdf":
        warnings.append(PDF_NOTICE)
    return {
        **report,
        "source": str(source),
        "output": str(output),
        "source_format": source_format[1:],
        "output_format": destination_format[1:],
        "warnings": warnings,
    }


def main(argv=None):
    args = parse_args(argv)
    try:
        report = convert(args)
    except ImportError:
        print("缺少 Python 依赖，请按 README 安装后重试。", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, BadZipFile) as error:
        print(f"转换失败。{error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"已生成 {report['output']}")
        for warning in report["warnings"]:
            print(warning, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
