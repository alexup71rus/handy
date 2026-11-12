import { formatNumber, loadData, showError, showReport } from '../shared/report.js';

const byId = id => document.getElementById(id);
const svg = byId('chart');
const slider = byId('point');
const all = byId('all-points');
const recent = byId('recent-points');
const line = byId('line-chart');
const bar = byId('bar-chart');
const donut = byId('donut-chart');
const donutKey = byId('donut-key');
const compact = new Intl.NumberFormat('ru-RU', { notation: 'compact', maximumSignificantDigits: 3 });
const scientific = new Intl.NumberFormat('ru-RU', { notation: 'scientific', maximumSignificantDigits: 2 });
const percent = new Intl.NumberFormat('ru-RU', { style: 'percent', maximumFractionDigits: 1 });
const ringColors = [
  '#197652', '#376b9d', '#af6d1d', '#895294', '#ad4d52', '#257983',
  '#745938', '#5b69b1', '#757c2d', '#ab576e', '#426873', '#9b5c33',
];

function element(tag, attrs = {}, text = '') {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text) node.textContent = text;
  return node;
}

function scale(values) {
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values) || (min === 0 ? 1 : 0);
  const target = (max - min) / 4;
  const base = 10 ** Math.floor(Math.log10(target)) || Number.MIN_VALUE;
  const step = ([1, 2, 5].find(factor => factor * base >= target) ?? 10) * base;
  const first = Math.floor(min / step);
  const last = Math.ceil(max / step);
  const ticks = Array.from({ length: last - first + 1 }, (_, i) => Number(((first + i) * step).toPrecision(12)));
  return ticks;
}

function setup(data) {
  if (data.chartType !== undefined && !['line', 'bar', 'donut'].includes(data.chartType)) {
    throw new Error('В chartType укажи line, bar или donut.');
  }
  if (typeof data.label !== 'string' || !data.label.trim() ||
      (data.unit !== undefined && typeof data.unit !== 'string') ||
      !Array.isArray(data.points) || data.points.length < 1 || data.points.length > 366 ||
      data.points.some(point => !point || typeof point.label !== 'string' || !point.label.trim() ||
        !Number.isFinite(point.value) || Math.abs(point.value) > 1e12)) {
    throw new Error('Проверь label и points в data.json. Нужно от 1 до 366 точек с подписью и числом.');
  }
  const valueText = value => `${formatNumber(value)}${data.unit ? ` ${data.unit}` : ''}`;
  const guide = element('line', { class: 'selected-guide' });
  const dot = element('circle', { class: 'selected-dot', r: 5 });
  const share = element('text', { class: 'donut-share', 'text-anchor': 'middle' });
  let chartType = data.chartType ?? 'line';
  let marks = [], legendButtons = [];
  let points, coordinates, selected, width, total;

  function select(index) {
    marks[selected]?.classList.remove('selected');
    legendButtons[selected]?.setAttribute('aria-pressed', 'false');
    selected = index;
    marks[index]?.classList.add('selected');
    legendButtons[index]?.setAttribute('aria-pressed', 'true');
    const { label: category, value } = points[index];
    let label = `${category} · ${valueText(value)}`;
    if (chartType === 'donut') {
      share.textContent = percent.format(value / total);
      label += ` · ${share.textContent}`;
    } else {
      const { x, y } = coordinates[index];
      guide.setAttribute('x1', x);
      guide.setAttribute('x2', x);
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', y);
    }
    slider.value = index;
    slider.setAttribute('aria-valuetext', label);
    byId('selection').textContent = label;
  }

  function drawDonut(height, title) {
    const x = width / 2, y = height / 2;
    const size = Math.min(width, height);
    const radius = size * .31;
    const pointAt = fraction => {
      const angle = fraction * 2 * Math.PI;
      return `${x + radius * Math.sin(angle)} ${y - radius * Math.cos(angle)}`;
    };
    let offset = 0;
    marks = points.map(({ value }, index) => {
      const length = value / total;
      const start = pointAt(offset);
      offset += length;
      const attrs = { class: 'donut-segment', stroke: ringColors[index], 'stroke-width': size * .14 };
      const segment = length === 1
        ? element('circle', { ...attrs, cx: x, cy: y, r: radius })
        : element('path', { ...attrs, d: length
          ? `M${start} A${radius} ${radius} 0 ${length > .5 ? 1 : 0} 1 ${pointAt(offset)}`
          : '' });
      segment.addEventListener('click', () => select(index));
      return segment;
    });
    share.setAttribute('x', x);
    share.setAttribute('y', y + 10);
    svg.replaceChildren(title, ...marks, share);
    legendButtons = [];
    const entries = points.map(({ label, value }, index) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('aria-pressed', 'false');
      const swatch = element('svg', { width: 10, height: 10, viewBox: '0 0 10 10', 'aria-hidden': 'true' });
      swatch.append(element('circle', { cx: 5, cy: 5, r: 5, fill: ringColors[index] }));
      const name = document.createElement('span');
      name.textContent = label;
      const proportion = document.createElement('span');
      proportion.className = 'key-share';
      proportion.textContent = percent.format(value / total);
      button.append(swatch, name, proportion);
      button.addEventListener('click', () => select(index));
      legendButtons.push(button);
      item.append(button);
      return item;
    });
    donutKey.replaceChildren(...entries);
    select(selected);
  }

  function draw() {
    width = Math.max(230, svg.clientWidth);
    const height = width < 520 ? 235 : 310;
    const barMode = chartType === 'bar';
    const donutMode = chartType === 'donut';
    line.setAttribute('aria-pressed', chartType === 'line');
    bar.setAttribute('aria-pressed', barMode);
    donut.setAttribute('aria-pressed', donutMode);
    donutKey.hidden = !donutMode;
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const chartName = { line: 'линейный', bar: 'столбчатый', donut: 'кольцевой' }[chartType];
    const title = element('title', { id: 'chart-label' }, `${data.label} · ${chartName} график · точек ${points.length}`);
    if (donutMode) {
      drawDonut(height, title);
      return;
    }
    legendButtons = [];
    const top = 18;
    const bottom = height - 32;
    const left = width < 520 ? 52 : 69;
    const right = width - 17;
    const slotWidth = (right - left) / points.length;
    const ticks = scale(points.map(point => point.value));
    const min = ticks[0];
    const max = ticks.at(-1);
    const xAt = barMode
      ? index => left + (index + .5) * slotWidth
      : index => points.length === 1 ? (left + right) / 2 : left + index * (right - left) / (points.length - 1);
    const yAt = value => bottom - (value - min) * (bottom - top) / (max - min);
    coordinates = points.map((point, index) => ({ x: xAt(index), y: yAt(point.value) }));
    const nodes = [title];
    for (const tick of ticks) {
      const y = yAt(tick);
      const className = barMode && tick === 0 ? 'grid-line zero-line' : 'grid-line';
      nodes.push(element('line', { class: className, x1: left, x2: right, y1: y, y2: y }));
      nodes.push(element('text', { class: 'axis-label', x: left - 9, y: y + 4, 'text-anchor': 'end' },
        tick !== 0 && Math.abs(tick) < .001 ? scientific.format(tick) : compact.format(tick)));
    }
    const labels = Math.min(points.length, Math.max(2, Math.floor((right - left) / 101) + 1));
    const xLabels = [];
    for (let i = 0; i < labels; i++) {
      const index = labels === 1 ? 0 : Math.round(i * (points.length - 1) / (labels - 1));
      const chars = [...points[index].label];
      const label = chars.length > 14 ? chars.slice(0, 13).join('') + '…' : chars.join('');
      const anchor = labels === 1 ? 'middle' : i === 0 ? 'start' : i === labels - 1 ? 'end' : 'middle';
      xLabels.push(element('text', { class: 'axis-label x-label', x: xAt(index), y: bottom + 22, 'text-anchor': anchor }, label));
    }
    nodes.push(...xLabels);
    const baseline = yAt(0);
    if (barMode) {
      const barWidth = Math.min(52, slotWidth * .76);
      marks = coordinates.map(({ x, y }) => element('rect', {
        class: 'series-bar',
        x: x - barWidth / 2,
        y: Math.min(y, baseline),
        width: barWidth,
        height: Math.abs(y - baseline),
      }));
      nodes.push(...marks);
    } else {
      marks = [];
      const definitions = element('defs');
      const gradient = element('linearGradient', { id: 'chart-fill', x2: '0%', y2: '100%' });
      gradient.append(element('stop', { offset: '0%', 'stop-color': '#178461', 'stop-opacity': '.24' }),
        element('stop', { offset: '100%', 'stop-color': '#178461', 'stop-opacity': '.02' }));
      definitions.append(gradient);
      const path = coordinates.map(({ x, y }, i) => `${i ? 'L' : 'M'}${x} ${y}`).join(' ');
      nodes.push(definitions,
        element('path', { d: `${path} L${coordinates.at(-1).x} ${baseline} L${coordinates[0].x} ${baseline} Z`, fill: 'url(#chart-fill)' }),
        element('path', { d: path, class: 'series-line' }));
    }
    guide.setAttribute('y1', top);
    guide.setAttribute('y2', bottom);
    svg.replaceChildren(...nodes, guide, dot);
    const lastLabelStart = xLabels.at(-1).getBBox().x;
    let previousLabelEnd = -Infinity;
    for (const label of xLabels.slice(0, -1)) {
      const { x, width } = label.getBBox();
      if (x < previousLabelEnd + 8 || x + width > lastLabelStart - 8) label.remove();
      else previousLabelEnd = x + width;
    }
    select(selected);
  }

  function setChartType(type) {
    if (chartType === type) return;
    chartType = type;
    draw();
  }

  function setPeriod(useRecent) {
    points = useRecent ? data.points.slice(-7) : data.points;
    const values = points.map(point => point.value);
    total = values.reduce((sum, value) => sum + value, 0);
    donut.disabled = points.length > ringColors.length || total <= 0 || values.some(value => value < 0);
    byId('donut-note').hidden = !donut.disabled;
    if (donut.disabled && chartType === 'donut') chartType = 'line';
    byId('last').textContent = valueText(values.at(-1));
    byId('minimum').textContent = valueText(Math.min(...values));
    byId('maximum').textContent = valueText(Math.max(...values));
    all.setAttribute('aria-pressed', !useRecent);
    recent.setAttribute('aria-pressed', useRecent);
    slider.max = points.length - 1;
    slider.disabled = points.length === 1;
    const rows = points.map(point => {
      const row = document.createElement('tr');
      row.insertCell().textContent = point.label;
      const value = row.insertCell();
      value.className = 'number';
      value.textContent = valueText(point.value);
      return row;
    });
    byId('point-rows').replaceChildren(...rows);
    selected = points.length - 1;
    draw();
  }

  function pick(event) {
    if (chartType === 'donut' || points.length === 1) return;
    const rect = svg.getBoundingClientRect();
    const x = (event.clientX - rect.left) * width / rect.width;
    const first = coordinates[0].x, last = coordinates.at(-1).x;
    const fraction = Math.min(1, Math.max(0, (x - first) / (last - first)));
    const index = Math.round(fraction * (points.length - 1));
    if (index !== selected) select(index);
  }

  byId('series').textContent = data.label;
  byId('value-column').textContent = data.label;
  byId('period').hidden = data.points.length <= 7;
  showReport(data);
  setPeriod(false);
  line.addEventListener('click', () => setChartType('line'));
  bar.addEventListener('click', () => setChartType('bar'));
  donut.addEventListener('click', () => setChartType('donut'));
  all.addEventListener('click', () => setPeriod(false));
  recent.addEventListener('click', () => setPeriod(true));
  slider.addEventListener('input', () => select(Number(slider.value)));
  svg.addEventListener('pointermove', event => { if (event.pointerType === 'mouse') pick(event); });
  svg.addEventListener('click', pick);
  new ResizeObserver(() => { if (Math.max(230, svg.clientWidth) !== width) draw(); }).observe(svg);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
