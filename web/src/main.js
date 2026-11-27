import './style.css';
import demoBookUrl from '../../examples/demo-zh-tw.epub?url&no-inline';

const MAX_BYTES = 50 * 1024 * 1024;
const byId = (id) => document.getElementById(id);
const form = byId('converter');
const dropzone = byId('dropzone');
const input = byId('file-input');
const choose = byId('choose');
const demo = byId('demo');
const convert = byId('convert');
const keepQuotes = byId('keep-quotes');
const mainlandTerms = byId('mainland-terms');
const sourceTitle = byId('source-title');
const fileSize = byId('file-size');
const result = byId('result');
const status = byId('status');
const progress = byId('progress');
const warnings = byId('warnings');
const download = byId('download');
let book = null;
let busy = false;
let downloadUrl = null;

function clearResult() {
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = null;
  download.removeAttribute('href');
  download.hidden = true;
  warnings.replaceChildren();
  warnings.hidden = true;
  result.removeAttribute('data-kind');
  status.textContent = '';
}

function notify(message, kind) {
  status.textContent = message;
  result.dataset.kind = kind;
}

function updateControls(isBusy) {
  busy = isBusy;
  form.setAttribute('aria-busy', String(isBusy));
  choose.disabled = demo.disabled = keepQuotes.disabled = mainlandTerms.disabled = isBusy;
  convert.disabled = isBusy || !book;
}

function select(file) {
  if (busy || !file) return;
  clearResult();
  book = null;
  if (!/\.epub$/i.test(file.name)) {
    notify('请选择 .epub 文件。', 'error');
  } else if (!file.size || file.size > MAX_BYTES) {
    notify(file.size ? '文件超过 50 MB。' : '文件是空的，请选择另一份 EPUB。', 'error');
  } else {
    book = file;
  }
  sourceTitle.textContent = book ? book.name : '拖入 EPUB 文件';
  sourceTitle.title = book?.name ?? '';
  fileSize.hidden = !book;
  fileSize.textContent = book ? (book.size < 1024 * 1024
    ? `${Math.max(1, Math.round(book.size / 1024))} KB`
    : `${(book.size / (1024 * 1024)).toFixed(1)} MB`) : '';
  choose.textContent = book ? '更换文件' : '选择文件';
  updateControls(false);
}

choose.addEventListener('click', () => input.click());
input.addEventListener('change', () => {
  select(input.files[0]);
  input.value = '';
});

for (const name of ['dragover', 'drop']) {
  window.addEventListener(name, (event) => {
    if (event.dataTransfer?.types.includes('Files')) event.preventDefault();
  });
}
dropzone.addEventListener('dragover', (event) => {
  if (busy || !event.dataTransfer?.types.includes('Files')) return;
  event.dataTransfer.dropEffect = 'copy';
  dropzone.dataset.over = '';
});
dropzone.addEventListener('dragleave', (event) => {
  if (!dropzone.contains(event.relatedTarget)) delete dropzone.dataset.over;
});
dropzone.addEventListener('drop', (event) => {
  delete dropzone.dataset.over;
  if (busy) return;
  if (event.dataTransfer?.files.length > 1) {
    clearResult();
    notify('每次请选择一本 EPUB。', 'error');
  } else {
    select(event.dataTransfer?.files[0]);
  }
});

demo.addEventListener('click', async () => {
  clearResult();
  updateControls(true);
  notify('正在加载样书…', 'busy');
  try {
    const response = await fetch(demoBookUrl);
    if (!response.ok) throw new Error('样书暂时无法加载。');
    const sample = new File([await response.blob()], '等一盞港燈.epub', { type: 'application/epub+zip' });
    updateControls(false);
    select(sample);
    convert.focus();
  } catch {
    notify('样书暂时无法加载。', 'error');
    updateControls(false);
  }
});

for (const option of [keepQuotes, mainlandTerms]) option.addEventListener('change', clearResult);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !book) return;
  clearResult();
  updateControls(true);
  progress.hidden = false;
  progress.value = 0;
  notify('正在转换 EPUB…', 'busy');
  try {
    const { convertEpub } = await import('./epub.js');
    const output = await convertEpub(book, {
      keepQuotes: keepQuotes.checked,
      mainlandTerms: mainlandTerms.checked,
      onProgress: (fraction) => { progress.value = Math.max(0, Math.min(1, fraction)); },
    });
    downloadUrl = URL.createObjectURL(output.blob);
    download.href = downloadUrl;
    download.download = `${book.name.replace(/\.epub$/i, '')}（简体横排）.epub`;
    download.hidden = false;
    for (const message of output.warnings ?? []) {
      const item = document.createElement('li');
      item.textContent = message;
      warnings.append(item);
    }
    warnings.hidden = warnings.childElementCount === 0;
    notify('转换完成', 'success');
    download.focus();
  } catch (error) {
    notify(error instanceof Error && error.message ? error.message : '转换失败，请检查 EPUB 文件。', 'error');
  } finally {
    progress.hidden = true;
    updateControls(false);
  }
});
