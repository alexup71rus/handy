import { formatNumber, loadData, showError, showReport } from '../shared/report.js';

const byId = id => document.getElementById(id);
const collator = new Intl.Collator('ru-RU', { numeric: true, sensitivity: 'base' });
const date = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
const normalize = text => text.normalize('NFKC').toLocaleLowerCase('ru-RU');
const pageSize = 24;

function isDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return false;
  const day = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(day.getTime()) && day.toISOString().slice(0, 10) === value;
}

const types = {
  text: { valid: value => typeof value === 'string', format: value => value, compare: collator.compare },
  number: { valid: Number.isFinite, format: formatNumber, compare: (a, b) => a - b },
  date: { valid: isDate, format: value => date.format(new Date(`${value}T00:00:00Z`)), compare: collator.compare },
};

function setup(data) {
  if (data.density !== undefined && !['comfortable', 'compact'].includes(data.density)) {
    throw new Error('В density укажи comfortable или compact.');
  }
  if (!Array.isArray(data.columns) || data.columns.length < 1 || data.columns.length > 16 ||
      data.columns.some(column => !column || typeof column.key !== 'string' || !column.key.trim() ||
        typeof column.label !== 'string' || !column.label.trim() || typeof column.type !== 'string' ||
        !Object.hasOwn(types, column.type) ||
        (column.unit !== undefined && typeof column.unit !== 'string')) ||
      new Set(data.columns.map(column => column.key)).size !== data.columns.length) {
    throw new Error('Проверь columns в data.json. Нужно от 1 до 16 столбцов с уникальными ключами и типами text, number или date.');
  }
  if (!Array.isArray(data.rows) || data.rows.length > 5000) {
    throw new Error('В rows нужен список до 5 000 записей.');
  }
  const columns = data.columns;
  const rows = data.rows.map((row, rowIndex) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error(`Проверь строку ${rowIndex + 1} в rows.`);
    const values = columns.map(column => Object.hasOwn(row, column.key) ? row[column.key] : null);
    const labels = values.map((value, index) => {
      if (value === null) return '—';
      const { type, label, unit } = columns[index];
      if (!types[type].valid(value)) throw new Error(`Проверь значение «${label}» в строке ${rowIndex + 1}.`);
      return types[type].format(value) + (unit ? ` ${unit}` : '');
    });
    const fields = values.map((value, index) => normalize([value, labels[index]].join(' ')));
    return { values, labels, fields, search: fields.join(' ') };
  });

  const filter = byId('filter');
  const filterColumn = byId('filter-column');
  const body = byId('rows');
  const header = byId('columns');
  const viewport = byId('table-scroll');
  const table = byId('data-table');
  const comfortable = byId('comfortable-table');
  const compact = byId('compact-table');
  const previous = byId('previous-page');
  const next = byId('next-page');
  let sortIndex = -1;
  let direction = 1;
  let page = 0;

  const headings = columns.map(({ label, type }, index) => {
    const cell = document.createElement('th');
    cell.scope = 'col';
    if (type === 'number') cell.className = 'number';
    const button = document.createElement('button');
    button.type = 'button';
    const icon = document.createElement('span');
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = '↕';
    button.append(label, icon);
    button.addEventListener('click', () => {
      if (sortIndex === index) direction *= -1;
      else { sortIndex = index; direction = 1; }
      headings.forEach(({ cell, icon }, i) => {
        if (i === sortIndex) cell.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending');
        else cell.removeAttribute('aria-sort');
        icon.textContent = i !== sortIndex ? '↕' : direction === 1 ? '↑' : '↓';
      });
      page = 0;
      render();
    });
    cell.append(button);
    return { cell, icon };
  });

  function updateScrollHint() {
    const scrollable = viewport.scrollWidth > viewport.clientWidth;
    byId('scroll-hint').hidden = !scrollable;
    viewport.tabIndex = scrollable ? 0 : -1;
    if (scrollable) viewport.setAttribute('aria-describedby', 'scroll-hint');
    else viewport.removeAttribute('aria-describedby');
  }

  function setDensity(useCompact) {
    table.classList.toggle('compact', useCompact);
    comfortable.setAttribute('aria-pressed', !useCompact);
    compact.setAttribute('aria-pressed', useCompact);
    updateScrollHint();
  }

  function render() {
    const words = normalize(filter.value).trim().split(/\s+/).filter(Boolean);
    const columnIndex = filterColumn.value === '' ? null : Number(filterColumn.value);
    const matches = rows.filter(row => {
      const searchable = columnIndex === null ? row.search : row.fields[columnIndex];
      return words.every(word => searchable.includes(word));
    });
    if (sortIndex !== -1) {
      const compare = types[columns[sortIndex].type].compare;
      matches.sort((a, b) => {
        const left = a.values[sortIndex], right = b.values[sortIndex];
        if (left === null || right === null) return Number(left === null) - Number(right === null);
        return direction * compare(left, right);
      });
    }
    const start = page * pageSize;
    const visible = matches.slice(start, start + pageSize);
    body.replaceChildren();
    if (!visible.length) {
      const empty = body.insertRow().insertCell();
      empty.colSpan = columns.length;
      empty.textContent = rows.length ? 'По этому запросу записей нет.' : 'В файле пока нет записей.';
    }
    for (const { labels } of visible) {
      const row = body.insertRow();
      labels.forEach((label, index) => {
        const cell = row.insertCell();
        if (columns[index].type === 'number') cell.className = 'number';
        cell.textContent = label;
      });
    }
    byId('row-count').textContent = visible.length ?
      `Показано ${start + 1}–${start + visible.length} из ${matches.length}` : 'Показано 0 записей';
    byId('pagination').hidden = matches.length <= pageSize;
    previous.disabled = page === 0;
    next.disabled = start + pageSize >= matches.length;
    updateScrollHint();
  }

  header.replaceChildren(...headings.map(({ cell }) => cell));
  columns.forEach(({ label }, index) => filterColumn.add(new Option(label, String(index))));
  table.classList.toggle('wide', columns.length > 3);
  byId('table-caption').textContent = data.title;
  comfortable.addEventListener('click', () => setDensity(false));
  compact.addEventListener('click', () => setDensity(true));
  filter.addEventListener('input', () => { page = 0; render(); });
  filterColumn.addEventListener('change', () => { page = 0; render(); });
  previous.addEventListener('click', () => { page--; render(); });
  next.addEventListener('click', () => { page++; render(); });
  showReport(data);
  setDensity(data.density === 'compact');
  render();
  new ResizeObserver(updateScrollHint).observe(viewport);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
