import { loadData, showError, showReport } from '../shared/report.js';

const find = selector => document.querySelector(selector);

function make(tag, text, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function copyCommand(text) {
  find('#schedule-command').hidden = false;
  const field = find('#command-value');
  field.value = text;
  const status = find('#command-status');
  try {
    await navigator.clipboard.writeText(text);
    status.textContent = 'Команда скопирована. Вставь её в чат Handy и подтверди действие.';
  } catch {
    field.focus();
    field.select();
    status.textContent = 'Скопируй выделенную команду и вставь её в чат Handy.';
  }
}

function jobCard(job) {
  const card = make('li', '', 'card schedule-job');
  const header = make('div', '', 'schedule-job-head');
  header.append(make('h2', job.title), make('span', job.state, job.enabled ? 'schedule-on' : 'schedule-off'));
  const details = make('dl');
  for (const [key, value] of [
    ['Сайт', job.host], ['Интервал', job.interval], ['Уведомления', job.direction],
    ['Цена', job.price], ['Следующий замер (UTC)', job.next], ['ID', job.id],
  ]) {
    const item = make('div');
    item.append(make('dt', key), make('dd', value));
    details.append(item);
  }
  const actions = make('div', '', 'schedule-actions');
  for (const [label, verb] of [[job.enabled ? 'Пауза' : 'Включить', job.enabled ? 'Отключи' : 'Включи'],
    ['Изменить', 'Измени'], ['Удалить', 'Удали']]) {
    const button = make('button', label);
    button.type = 'button';
    button.title = 'Скопировать команду для чата';
    button.setAttribute('aria-label', `${label} задачу ${job.title}. Скопировать команду`);
    button.addEventListener('click', () => copyCommand(`${verb} задачу ${job.id}`));
    actions.append(button);
  }
  card.append(header, details);
  if (job.error) card.append(make('p', job.error, 'schedule-error'));
  card.append(actions);
  return card;
}

function setup(data) {
  if (!Array.isArray(data.jobs) || data.jobs.length > 100 || data.jobs.some(job =>
    typeof job?.title !== 'string' || typeof job.enabled !== 'boolean' || !/^[a-f0-9]{12}$/.test(job.id))) {
    throw new Error('Не удалось прочитать снимок расписания. Запроси новый артефакт в чате.');
  }
  const worker = find('#worker-status');
  worker.textContent = data.worker_running ? 'Фоновый исполнитель работает' : data.jobs.some(job => job.enabled)
    ? 'Фоновый исполнитель недоступен. Включи задачу повторно в чате.' : 'Сейчас нет активных проверок';
  if (data.partial) worker.textContent += '. Показаны первые 100 задач';
  const filter = find('#job-filter');
  const list = find('#jobs');
  const empty = find('#jobs-empty');
  function render() {
    const jobs = data.jobs.filter(job => filter.value === 'all' || job.enabled === (filter.value === 'on'));
    list.replaceChildren(...jobs.map(jobCard));
    empty.textContent = data.jobs.length ? 'Задач с таким статусом нет.' : 'В этой папке пока нет запланированных задач.';
    empty.hidden = jobs.length > 0;
  }
  filter.addEventListener('change', render);
  render();
  showReport({ ...data, description: 'Посмотри цену, следующий замер и состояние задач. Кнопки готовят команды, которые можно подтвердить в чате Handy.' });
}

loadData('data.json').then(setup).catch(showError);
