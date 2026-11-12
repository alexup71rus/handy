import { loadData, showError, showReport } from '../shared/report.js';

function setup(data) {
  if (data.timeZone !== undefined && (typeof data.timeZone !== 'string' || !data.timeZone.trim())) {
    throw new Error('Укажи часовой пояс в поле timeZone, например Europe/Moscow.');
  }
  const timeZone = data.timeZone?.trim();
  let hours;
  let calendar;
  try {
    hours = new Intl.DateTimeFormat('ru-RU', {
      timeZone, hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    calendar = new Intl.DateTimeFormat('ru-RU', {
      timeZone, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
    });
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
    throw new Error('Проверь часовой пояс в поле timeZone. Пример — Europe/Moscow.');
  }

  const time = document.querySelector('#clock-time');
  const date = document.querySelector('#clock-date');
  document.querySelector('#clock-zone').textContent = timeZone ?? hours.resolvedOptions().timeZone ?? 'Местное время';
  let timer;
  function tick() {
    clearTimeout(timer);
    const now = new Date();
    time.dateTime = now.toISOString();
    time.textContent = hours.format(now);
    date.textContent = calendar.format(now);
    if (!document.hidden) timer = setTimeout(tick, 1000 - now.getMilliseconds());
  }
  document.addEventListener('visibilitychange', tick);
  tick();
  showReport(data);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
