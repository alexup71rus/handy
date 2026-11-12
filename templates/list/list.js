import { loadData, showError, showReport } from '../shared/report.js';

const pageSize = 12;
const searchKey = text => text.toLocaleLowerCase('ru-RU');

function normalizeItems(data) {
  if (!Array.isArray(data.items) || data.items.length > 5000 ||
      (data.checklist !== undefined && typeof data.checklist !== 'boolean')) {
    throw new Error('Проверь поля items и checklist в data.json. Список вмещает до 5 000 пунктов.');
  }
  return data.items.map(entry => {
    const item = typeof entry === 'string' ? { label: entry } : entry;
    if (!item || typeof item.label !== 'string' || !item.label.trim() ||
        (item.detail !== undefined && typeof item.detail !== 'string') ||
        (item.checked !== undefined && typeof item.checked !== 'boolean')) {
      throw new Error('Для каждого пункта укажи текст или объект с полем label.');
    }
    return { ...item, search: searchKey(`${item.label} ${item.detail ?? ''}`) };
  });
}

function setup(data) {
  const items = normalizeItems(data);
  const list = document.querySelector('#list-items');
  const filter = document.querySelector('#list-filter');
  const empty = document.querySelector('#list-empty');
  const count = document.querySelector('#list-count');
  const progress = document.querySelector('#checked-count');
  const pagination = document.querySelector('#list-pagination');
  const previous = document.querySelector('#previous-page');
  const next = document.querySelector('#next-page');
  let page = 0;
  let shown = items;
  let checked = items.filter(item => item.checked).length;

  function showProgress() {
    progress.textContent = `Отмечено ${checked} из ${items.length}`;
  }

  function makeRow(item) {
    const row = document.createElement('li');
    const content = document.createElement(data.checklist ? 'label' : 'div');
    const text = document.createElement('span');
    const title = document.createElement('span');
    row.className = 'list-row';
    text.className = 'list-copy';
    title.className = 'list-label';
    title.textContent = item.label;
    text.append(title);
    if (item.detail) {
      const detail = document.createElement('span');
      detail.className = 'quiet';
      detail.textContent = item.detail;
      text.append(detail);
    }
    if (data.checklist) {
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = Boolean(item.checked);
      box.addEventListener('change', () => {
        checked += Number(box.checked) - Number(Boolean(item.checked));
        item.checked = box.checked;
        showProgress();
      });
      content.append(box);
    }
    content.append(text);
    row.append(content);
    return row;
  }

  function render() {
    const start = page * pageSize;
    const end = Math.min(start + pageSize, shown.length);
    list.replaceChildren(...shown.slice(start, end).map(makeRow));
    empty.hidden = shown.length > 0;
    empty.textContent = items.length ? 'Совпадений нет.' : 'Список пока пуст.';
    count.textContent = shown.length ? `Показано ${start + 1}–${end} из ${shown.length}` : 'Показано 0';
    pagination.hidden = shown.length <= pageSize;
    previous.disabled = start === 0;
    next.disabled = end >= shown.length;
  }

  filter.addEventListener('input', () => {
    const terms = searchKey(filter.value).trim().split(/\s+/).filter(Boolean);
    shown = items.filter(item => terms.every(term => item.search.includes(term)));
    page = 0;
    render();
  });
  previous.addEventListener('click', () => { page -= 1; render(); });
  next.addEventListener('click', () => { page += 1; render(); });
  document.querySelector('#list-search').hidden = !items.length;
  progress.hidden = !data.checklist;
  if (data.checklist) showProgress();
  render();
  showReport(data);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
