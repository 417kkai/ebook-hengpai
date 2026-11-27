import { BlobReader, BlobWriter, TextReader, ZipReader, ZipWriter } from '@zip.js/zip.js/index-native.js';
import { Converter } from 'opencc-js/t2cn';
import postcss from 'postcss';

export const MAX_INPUT_BYTES = 50 * 1024 * 1024;
const MAX_EXPANDED_BYTES = 220 * 1024 * 1024;
const MAX_ENTRY_BYTES = 64 * 1024 * 1024;
const MAX_MARKUP_BYTES = 16 * 1024 * 1024;
const MAX_ENTRIES = 7500;
const EPUB_TYPE = 'application/epub+zip';
const LANGUAGE = 'zh-Hans-CN';
const CONTAINER_NS = 'urn:oasis:names:tc:opendocument:xmlns:container';
const OPF_NS = 'http://www.idpf.org/2007/opf';
const DC_NS = 'http://purl.org/dc/elements/1.1/';
const XHTML_NS = 'http://www.w3.org/1999/xhtml';
const SVG_NS = 'http://www.w3.org/2000/svg';
const XML_NS = 'http://www.w3.org/XML/1998/namespace';
const ENCRYPTION_NS = 'http://www.w3.org/2001/04/xmlenc#';
const FONT_OBFUSCATION = new Set([
  'http://www.idpf.org/2008/embedding',
  'http://ns.adobe.com/pdf/enc#RC',
]);
const DOCUMENT_TYPES = new Set(['application/xhtml+xml', 'application/x-dtbncx+xml', 'image/svg+xml']);
const HUMAN_ATTRIBUTES = ['alt', 'title', 'aria-label', 'aria-description', 'summary'];
const DC_ELEMENTS = new Set(['title', 'creator', 'subject', 'description', 'publisher', 'contributor', 'coverage', 'rights']);
const TEXT_META = new Set(['belongs-to-collection', 'file-as', 'alternate-script']);
const CSS_VERTICAL_PROPERTY = /^(?:-(?:webkit|epub|moz|ms|o)-)?(?:text-orientation|text-combine(?:-upright|-horizontal)?|glyph-orientation-vertical)$/u;
const CSS_WRITING_MODE = /^(?:-(?:webkit|epub|moz|ms|o)-)?writing-mode$/u;
const CORNER_QUOTES = new Map([['「', '“'], ['」', '”'], ['『', '‘'], ['』', '’']]);
const VERTICAL_FORMS = /[\uFE10-\uFE19\uFE30-\uFE44\uFE47\uFE48]/gu;
const VERTICAL_HORIZONTAL = [
  [0xfe10, '，、。：；！？〖〗…'],
  [0xfe30, '‥—–__（）｛｝〔〕【】《》〈〉「」『』'],
  [0xfe47, '［］'],
];
const NORMALIZED_PUNCTUATION = new Map(VERTICAL_HORIZONTAL.flatMap(([first, horizontal]) =>
  [...horizontal].map((char, index) => [String.fromCharCode(first + index), char])));
const ENTITIES = new Map();
const CONVERTERS = new Map();
const decoder = new TextDecoder();

class EpubError extends Error {}

function children(element, namespace, name) {
  return [...element.children].filter((child) => child.namespaceURI === namespace && child.localName === name);
}

function first(element, namespace, name) {
  return children(element, namespace, name)[0];
}

function pathIsUnsafe(path) {
  return /[\u0000-\u001f\u007f:\\]/u.test(path)
    || path.replace(/\/$/u, '').split('/').some((part) => part === '' || part === '.' || part === '..');
}

function manifestPath(base, href) {
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/iu.test(href)) return null;
  const path = decodeURIComponent(href.split(/[?#]/u, 1)[0]);
  if (!path || path.startsWith('/') || /[\u0000-\u001f\u007f:\\]/u.test(path)) {
    throw new EpubError('电子书包含无效的资源路径。');
  }
  const segments = base.split('/').slice(0, -1);
  for (const segment of path.split('/')) {
    if (segment === '..') {
      if (!segments.length) throw new EpubError('电子书包含越界的资源路径。');
      segments.pop();
    } else if (segment && segment !== '.') {
      segments.push(segment);
    }
  }
  if (!segments.length) throw new EpubError('电子书包含无效的资源路径。');
  return segments.join('/');
}

async function indexArchive(reader, fileSize) {
  const entries = new Map();
  let total = 0;
  for await (const entry of reader.getEntriesGenerator()) {
    if (entries.size >= MAX_ENTRIES) throw new EpubError('电子书内的文件太多，浏览器无法安全处理。');
    if (entries.has(entry.filename) || pathIsUnsafe(entry.filename)) {
      throw new EpubError('电子书包含重复或无效的文件路径。');
    }
    if (entry.encrypted) throw new EpubError('这本书含加密内容，请使用无 DRM 的 EPUB。');
    const { uncompressedSize, compressedSize } = entry;
    if (!Number.isSafeInteger(uncompressedSize) || uncompressedSize < 0
      || !Number.isSafeInteger(compressedSize) || compressedSize < 0 || compressedSize > fileSize) {
      throw new EpubError('电子书的压缩目录已损坏。');
    }
    total += uncompressedSize;
    if (uncompressedSize > MAX_ENTRY_BYTES || total > MAX_EXPANDED_BYTES) {
      throw new EpubError('这本书解压后的体积超过网页转换上限。');
    }
    entries.set(entry.filename, entry);
  }
  return entries;
}

async function readEntry(entry, limit = MAX_ENTRY_BYTES) {
  if (!entry || entry.directory) throw new EpubError('电子书缺少必要文件。');
  if (entry.uncompressedSize > limit) throw new EpubError('电子书中的单个文件太大。');
  const chunks = [];
  let bytes = 0;
  const destination = new WritableStream({
    write(chunk) {
      bytes += chunk.byteLength;
      if (bytes > limit || bytes > entry.uncompressedSize) {
        throw new EpubError('电子书的解压体积与压缩目录不一致。');
      }
      chunks.push(chunk);
    },
  });
  try {
    await entry.getData(destination, { checkSignature: true, useWebWorkers: false });
  } catch (error) {
    if (error instanceof EpubError) throw error;
    throw new EpubError('电子书中有文件损坏，无法读取。', { cause: error });
  }
  if (bytes !== entry.uncompressedSize) throw new EpubError('电子书的解压体积与压缩目录不一致。');
  return new Blob(chunks);
}

async function readText(entry) {
  const blob = await readEntry(entry, MAX_MARKUP_BYTES);
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const leading = decoder.decode(bytes.subarray(0, 256));
  const declared = /^\s*(?:<\?xml\s[^>]*?encoding\s*=\s*["']([^"']+)["']|@charset\s+["']([^"']+)["'])/iu.exec(leading);
  let encoding = declared?.[1] || declared?.[2] || 'utf-8';
  if (bytes[0] === 0xff && bytes[1] === 0xfe) encoding = 'utf-16le';
  if (bytes[0] === 0xfe && bytes[1] === 0xff) encoding = 'utf-16be';
  if (bytes[0] === 0x00 && bytes[1] === 0x3c) encoding = 'utf-16be';
  if (bytes[0] === 0x3c && bytes[1] === 0x00) encoding = 'utf-16le';
  try {
    return new TextDecoder(encoding, { fatal: true }).decode(bytes);
  } catch (error) {
    throw new EpubError('电子书包含浏览器无法读取的文本编码。', { cause: error });
  }
}

function normalizeHtmlEntity(entity) {
  if (['&amp;', '&lt;', '&gt;', '&apos;', '&quot;'].includes(entity)) return entity;
  if (!ENTITIES.has(entity)) {
    // Only the alphanumeric entity name, never ebook markup, enters the HTML parser.
    const cell = new DOMParser().parseFromString(`<textarea>${entity}</textarea>`, 'text/html');
    const value = cell.body.firstElementChild.textContent;
    ENTITIES.set(entity, value === entity ? entity : [...value].map((char) => `&#${char.codePointAt(0)};`).join(''));
  }
  return ENTITIES.get(entity);
}

function parseXml(source, ordinaryBookText = false) {
  if (/<!ENTITY\s|<!DOCTYPE[^>]*\[/iu.test(source)) {
    throw new EpubError('电子书包含不受支持的 XML 实体声明。');
  }
  const parser = new DOMParser();
  const parse = (text) => parser.parseFromString(text, 'application/xml');
  const invalid = (doc) => doc.getElementsByTagNameNS('http://www.mozilla.org/newlayout/xml/parsererror.xml', 'parsererror').length;
  let doc = parse(source);
  if (invalid(doc) && ordinaryBookText) {
    const parts = source.split(/(<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>)/u);
    const repaired = parts.map((part, i) => i % 2 ? part : part.replace(/&[a-z][a-z\d]{1,31};/giu, normalizeHtmlEntity)).join('');
    if (repaired !== source) doc = parse(repaired);
  }
  if (invalid(doc) || !doc.documentElement) throw new EpubError('电子书包含损坏的 XML 排版文件。');
  return doc;
}

function serializeXml(doc) {
  const result = new XMLSerializer().serializeToString(doc).replace(/^<\?xml\s[^?]*\?>\s*/iu, '');
  return '<?xml version="1.0" encoding="UTF-8"?>\n' + result;
}

async function checkProtection(entries, packageElement, metadata, spine) {
  const encryption = entries.get('META-INF/encryption.xml');
  if (encryption) {
    const xml = parseXml(await readText(encryption));
    const records = [...xml.getElementsByTagNameNS(ENCRYPTION_NS, 'EncryptedData')];
    if (!records.length || records.some((record) => !FONT_OBFUSCATION.has(
      first(record, ENCRYPTION_NS, 'EncryptionMethod')?.getAttribute('Algorithm'),
    ))) {
      throw new EpubError('这本书含加密内容，请使用无 DRM 的 EPUB。');
    }
  }
  const fixedLayout = (element) => element.getAttribute('property') === 'rendition:layout'
    && element.textContent.trim() === 'pre-paginated';
  const legacyFixed = (element) => ['fixed-layout', 'book-type'].includes(element.getAttribute('name'))
    && ['true', 'fixed-layout', 'comic'].includes(element.getAttribute('content')?.toLowerCase());
  const itemFixed = (element) => element.getAttribute('properties')?.split(/\s+/u).includes('rendition:layout-pre-paginated');
  if (children(metadata, OPF_NS, 'meta').some((element) => fixedLayout(element) || legacyFixed(element))
    || children(spine, OPF_NS, 'itemref').some(itemFixed)
    || packageElement.getAttribute('fixed-layout') === 'true') {
    throw new EpubError('这本 EPUB 使用固定版式，需要使用可重排的版本。');
  }
  for (const name of ['META-INF/com.apple.ibooks.display-options.xml', 'META-INF/com.kobobooks.display-options.xml']) {
    if (!entries.has(name)) continue;
    const displayOptions = parseXml(await readText(entries.get(name)));
    if ([...displayOptions.getElementsByTagNameNS('*', 'option')].some((option) =>
      option.getAttribute('name') === 'fixed-layout' && option.textContent.trim().toLowerCase() === 'true')) {
      throw new EpubError('这本 EPUB 使用固定版式，需要使用可重排的版本。');
    }
  }
}

async function readPackage(entries) {
  const mimetype = decoder.decode(await (await readEntry(entries.get('mimetype'), 128)).arrayBuffer()).trim();
  if (mimetype !== EPUB_TYPE) throw new EpubError('所选文件不是有效的 EPUB。');
  const container = parseXml(await readText(entries.get('META-INF/container.xml')));
  const rootfiles = [...container.getElementsByTagNameNS(CONTAINER_NS, 'rootfile')];
  if (rootfiles.length !== 1) throw new EpubError('目前支持只有一个排版版本的 EPUB。');
  const fullPath = rootfiles[0].getAttribute('full-path') || '';
  const path = entries.has(fullPath) ? fullPath : decodeURIComponent(fullPath);
  const doc = parseXml(await readText(entries.get(path)));
  const pkg = doc.documentElement;
  const version = pkg.getAttribute('version') || '';
  if (pkg.namespaceURI !== OPF_NS || pkg.localName !== 'package' || !/^[23]\.\d+$/u.test(version)) {
    throw new EpubError('目前支持 EPUB 2 和 EPUB 3。');
  }
  const metadata = first(pkg, OPF_NS, 'metadata');
  const spine = first(pkg, OPF_NS, 'spine');
  const manifest = first(pkg, OPF_NS, 'manifest');
  if (!metadata || !spine || !manifest) throw new EpubError('电子书缺少书目信息、资源清单或阅读顺序。');
  await checkProtection(entries, pkg, metadata, spine);

  const resources = new Map();
  const itemIds = new Set();
  for (const item of children(manifest, OPF_NS, 'item')) {
    const resource = manifestPath(path, item.getAttribute('href') || '');
    const id = item.getAttribute('id');
    if (!id || itemIds.has(id)) throw new EpubError('电子书资源清单包含缺失或重复的编号。');
    itemIds.add(id);
    if (resource === null) continue;
    if (!entries.has(resource) || entries.get(resource).directory || resource === path) {
      throw new EpubError('电子书缺少资源清单引用的文件。');
    }
    if (resources.has(resource) && resources.get(resource) !== item.getAttribute('media-type')) {
      throw new EpubError('电子书对同一文件声明了不同的格式。');
    }
    resources.set(resource, item.getAttribute('media-type'));
  }
  const itemrefs = children(spine, OPF_NS, 'itemref');
  if (!itemrefs.length || itemrefs.some((item) => !itemIds.has(item.getAttribute('idref')))) {
    throw new EpubError('电子书的阅读顺序缺少引用的章节。');
  }
  return { doc, pkg, path, version, metadata, spine, resources };
}

function rewriteCss(source, inline, warnings) {
  if (!source.trim()) return source;
  let parsed;
  try {
    parsed = postcss.parse(inline ? `x{${source}}` : source, { from: undefined });
    if (inline && (parsed.nodes.length !== 1 || parsed.first?.selector !== 'x')) {
      throw new Error('invalid inline CSS');
    }
  } catch {
    warnings.add('部分样式无法解析，已保留原样。');
    return inline ? source : source.replace(/^@charset\s+["'][^"']+["']/iu, '@charset "UTF-8"');
  }
  let changed = false;
  parsed.walkDecls((declaration) => {
    const key = declaration.prop.toLowerCase();
    if (CSS_WRITING_MODE.test(key)) {
      const mode = key === '-ms-writing-mode' ? 'lr-tb' : 'horizontal-tb';
      if (declaration.value !== mode) {
        declaration.value = mode;
        changed = true;
      }
    } else if (key === 'direction' || CSS_VERTICAL_PROPERTY.test(key)) {
      declaration.remove();
      changed = true;
    }
  });
  if (!inline) parsed.walkAtRules('charset', (declaration) => {
    declaration.params = '"UTF-8"';
    changed = true;
  });
  if (!changed) return source;
  const result = parsed.toString();
  return inline ? result.slice(2, -1) : result;
}

function chineseLanguage(value) {
  return /^(?:zh|zho|chi|cmn)(?:[-_]|$)/iu.test(value || '');
}

function languageOf(element) {
  return element.getAttributeNS(XML_NS, 'lang') || element.getAttribute('lang');
}

function rewriteLanguage(element) {
  if (chineseLanguage(element.getAttributeNS(XML_NS, 'lang'))) element.setAttributeNS(XML_NS, 'xml:lang', LANGUAGE);
  if (chineseLanguage(element.getAttribute('lang'))) element.setAttribute('lang', LANGUAGE);
}

function rewriteContent(doc, media, epub3, convertText, warnings) {
  const root = doc.documentElement;
  const isXhtml = media === 'application/xhtml+xml' && root.namespaceURI === XHTML_NS && root.localName === 'html';
  if (isXhtml) {
    const language = languageOf(root);
    root.setAttribute('dir', 'ltr');
    if (!language || chineseLanguage(language)) {
      root.setAttributeNS(XML_NS, 'xml:lang', LANGUAGE);
      if (epub3 || root.hasAttribute('lang')) root.setAttribute('lang', LANGUAGE);
    }
  }
  let bodyText = false;
  const pending = [{ node: root, language: null, inBody: false }];
  while (pending.length) {
    const { node, language, inBody } = pending.pop();
    if (node.nodeType === 3 || node.nodeType === 4) {
      if (inBody && /[\p{L}\p{N}]/u.test(node.data)) bodyText = true;
      if (!language || chineseLanguage(language)) node.data = convertText(node.data);
      continue;
    }
    if (node.nodeType !== 1) continue;
    const activeLanguage = languageOf(node) || language;
    const textAllowed = !activeLanguage || chineseLanguage(activeLanguage);
    const body = inBody || (node.namespaceURI === XHTML_NS && node.localName === 'body');
    rewriteLanguage(node);
    if (textAllowed && node.getAttribute('dir') === 'rtl') node.setAttribute('dir', 'ltr');
    if (textAllowed) {
      for (const attribute of HUMAN_ATTRIBUTES) {
        if (node.hasAttribute(attribute)) node.setAttribute(attribute, convertText(node.getAttribute(attribute)));
      }
    }
    if (node.hasAttribute('style')) {
      node.setAttribute('style', rewriteCss(node.getAttribute('style'), true, warnings));
    }
    if (node.namespaceURI === SVG_NS && node.hasAttribute('writing-mode')) {
      node.setAttribute('writing-mode', 'lr-tb');
    }
    if (node.localName === 'style') {
      node.textContent = rewriteCss(node.textContent, false, warnings);
      continue;
    }
    if (node.localName === 'script' || (node.namespaceURI === SVG_NS && node.localName === 'metadata')) continue;
    for (let child = node.lastChild; child; child = child.previousSibling) {
      pending.push({ node: child, language: activeLanguage, inBody: body });
    }
  }
  if (isXhtml) {
    const head = first(root, XHTML_NS, 'head');
    const fallback = 'html { writing-mode: horizontal-tb; }';
    if (head && !children(head, XHTML_NS, 'style').some((style) => style.textContent.trim() === fallback)) {
      const style = doc.createElementNS(XHTML_NS, 'style');
      style.setAttribute('type', 'text/css');
      style.textContent = fallback;
      head.append(style);
    }
    for (const meta of head ? children(head, XHTML_NS, 'meta') : []) {
      if (meta.getAttribute('http-equiv')?.toLowerCase() === 'content-type' && meta.hasAttribute('content')) {
        meta.setAttribute('content', meta.getAttribute('content').replace(/charset\s*=\s*[^;\s]+/iu, 'charset=UTF-8'));
      }
      if (meta.hasAttribute('charset')) meta.setAttribute('charset', 'utf-8');
    }
  }
  return bodyText;
}

function rewritePackage(book, convertText, epub3) {
  const { pkg, metadata, spine } = book;
  if (epub3) {
    const language = languageOf(pkg);
    if (!language || chineseLanguage(language)) pkg.setAttributeNS(XML_NS, 'xml:lang', LANGUAGE);
    pkg.setAttribute('dir', 'ltr');
    spine.setAttribute('page-progression-direction', 'ltr');
  } else {
    spine.removeAttribute('page-progression-direction');
  }
  for (const item of metadata.children) {
    const language = languageOf(item);
    const textAllowed = !language || chineseLanguage(language);
    rewriteLanguage(item);
    if (item.namespaceURI === DC_NS && textAllowed) {
      if (item.localName === 'language' && chineseLanguage(item.textContent.trim())) item.textContent = LANGUAGE;
      if (DC_ELEMENTS.has(item.localName)) {
        item.textContent = convertText(item.textContent);
        const sortName = item.getAttributeNS(OPF_NS, 'file-as');
        if (sortName) item.setAttributeNS(OPF_NS, 'opf:file-as', convertText(sortName));
      }
    } else if (item.namespaceURI === OPF_NS && item.localName === 'meta') {
      const property = item.getAttribute('property');
      if (textAllowed && TEXT_META.has(property) && (property !== 'alternate-script' || language)) {
        item.textContent = convertText(item.textContent);
      }
      if (['calibre:series', 'calibre:title_sort'].includes(item.getAttribute('name')) && item.hasAttribute('content')) {
        item.setAttribute('content', convertText(item.getAttribute('content')));
      }
    }
  }
  const guide = first(pkg, OPF_NS, 'guide');
  for (const reference of guide ? children(guide, OPF_NS, 'reference') : []) {
    if (reference.hasAttribute('title')) reference.setAttribute('title', convertText(reference.getAttribute('title')));
  }
  return first(metadata, DC_NS, 'title')?.textContent.trim() || '';
}

export async function convertEpub(file, { keepQuotes = false, mainlandTerms = false, onProgress = () => {} } = {}) {
  if (file.size > MAX_INPUT_BYTES) throw new EpubError('请选择 50 MB 以内的 EPUB 文件。');
  if (file.size === 0) throw new EpubError('文件是空的，请选择一份 EPUB 电子书。');
  onProgress(0);
  const reader = new ZipReader(new BlobReader(file));
  try {
    const entries = await indexArchive(reader, file.size);
    const book = await readPackage(entries);
    const epub3 = book.version.startsWith('3.');
    const warnings = new Set();
    const locale = mainlandTerms ? 'twp' : 'tw';
    if (!CONVERTERS.has(locale)) CONVERTERS.set(locale, Converter({ from: locale, to: 'cn' }));
    const simplify = CONVERTERS.get(locale);
    let changes = 0;
    let documents = 0;
    let bodyText = false;
    const convertText = (text) => {
      if (!text.trim()) return text;
      const normalized = text.replace(VERTICAL_FORMS, (char) => NORMALIZED_PUNCTUATION.get(char) ?? char);
      const converted = simplify(normalized);
      const result = keepQuotes ? converted : converted.replace(/[「」『』]/gu, (quote) => CORNER_QUOTES.get(quote));
      if (result !== text) changes++;
      return result;
    };
    const title = rewritePackage(book, convertText, epub3);
    const zip = new ZipWriter(new BlobWriter(EPUB_TYPE), {
      extendedTimestamp: false,
      useWebWorkers: false,
    });
    await zip.add('mimetype', new TextReader(EPUB_TYPE), { level: 0, dataDescriptor: false });
    let processed = 0;
    for (const [name, entry] of entries) {
      if (name === 'mimetype') continue;
      const media = book.resources.get(name);
      if (entry.directory) {
        await zip.add(name, undefined, { directory: true });
      } else if (name === book.path) {
        await zip.add(name, new TextReader(serializeXml(book.doc)));
      } else if (media === 'text/css') {
        await zip.add(name, new TextReader(rewriteCss(await readText(entry), false, warnings)));
      } else if (DOCUMENT_TYPES.has(media)) {
        const doc = parseXml(await readText(entry), true);
        const thisHasText = rewriteContent(doc, media, epub3, convertText, warnings);
        bodyText ||= thisHasText;
        if (media !== 'application/x-dtbncx+xml') documents++;
        await zip.add(name, new TextReader(serializeXml(doc)));
      } else {
        const resource = await readEntry(entry);
        await zip.add(name, new BlobReader(resource), { level: entry.compressionMethod === 0 ? 0 : 5 });
      }
      processed++;
      onProgress(0.08 + (0.9 * processed) / Math.max(1, entries.size - 1));
    }
    if (!bodyText) warnings.add('未找到可选中的正文文字。图片中的文字会保持原样。');
    const blob = await zip.close();
    onProgress(1);
    return { blob, title, changes, documents, warnings: [...warnings] };
  } catch (error) {
    if (error instanceof EpubError) throw error;
    throw new EpubError('无法转换这份文件，请确认它是可正常打开的 EPUB。', { cause: error });
  } finally {
    await reader.close();
  }
}
