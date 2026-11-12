import { loadData, showError, showReport } from '../shared/report.js';

const MAX_SECONDS = 24 * 60 * 60;
const phases = {
  countdown: { label: 'Обратный отсчёт', finished: 'Время вышло.' },
  focus: { label: 'Фокус', finished: 'Фокус-сессия завершена.', setting: 'focusMinutes' },
  short: { label: 'Короткий перерыв', finished: 'Короткий перерыв завершён.', setting: 'shortBreakMinutes' },
  long: { label: 'Длинный перерыв', finished: 'Длинный перерыв завершён.', setting: 'longBreakMinutes' },
};

function timeParts(seconds) {
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60];
}

function timeText(milliseconds) {
  const [hours, minutes, seconds] = timeParts(Math.ceil(milliseconds / 1000));
  return (hours ? [hours, minutes, seconds] : [minutes, seconds]).map(value => String(value).padStart(2, '0')).join(':');
}

function audioSignal(toggle) {
  let audio;
  function enable() {
    if (!toggle.checked || !window.AudioContext) return;
    try {
      audio ??= new AudioContext();
      audio.resume().catch(() => {});
    } catch { /* The visual signal works without audio. */ }
  }
  function play() {
    if (!toggle.checked || audio?.state !== 'running') return;
    for (const [offset, frequency] of [[0, 740], [0.24, 980]]) {
      const time = audio.currentTime + offset;
      const tone = audio.createOscillator();
      const volume = audio.createGain();
      tone.frequency.value = frequency;
      volume.gain.setValueAtTime(0.15, time);
      volume.gain.exponentialRampToValueAtTime(0.001, time + 0.2);
      tone.connect(volume).connect(audio.destination);
      tone.start(time);
      tone.stop(time + 0.21);
    }
  }
  return { enable, play };
}

function setup(data) {
  if (!['countdown', 'pomodoro'].includes(data.mode)) throw new Error('Проверь режим таймера в data.json.');
  const pomodoro = data.mode === 'pomodoro';
  const fieldset = document.querySelector(pomodoro ? '#pomodoro-settings' : '#countdown-settings');
  const inputs = Array.from(fieldset.querySelectorAll('input'));
  const seconds = data.durationSeconds;
  if (!pomodoro && (!Number.isInteger(seconds) || seconds < 1 || seconds > MAX_SECONDS)) {
    throw new Error('Укажи durationSeconds от 1 до 86 400 в data.json.');
  }
  const initial = pomodoro ? inputs.map(input => data[input.name]) : timeParts(seconds);
  inputs.forEach((input, index) => { input.value = initial[index] ?? ''; });

  const report = document.querySelector('#report');
  const display = document.querySelector('#timer-time');
  const progress = document.querySelector('#timer-progress');
  const heading = document.querySelector('#timer-phase');
  const rounds = document.querySelector('#timer-rounds');
  const status = document.querySelector('#timer-status');
  const start = document.querySelector('#timer-toggle');
  const reset = document.querySelector('#timer-reset');
  const skip = document.querySelector('#timer-skip');
  const sound = document.querySelector('#timer-sound');
  const alarm = audioSignal(sound);
  let phase = pomodoro ? 'focus' : 'countdown';
  let completed = 0;
  let state = 'ready';
  let remaining = 0;
  let duration = 0;
  let deadline = null;
  let timeout;
  let settings;

  function readSettings() {
    if (inputs.some(input => !/^\d+$/.test(input.value) || !input.validity.valid)) return null;
    const values = Object.fromEntries(inputs.map(input => [input.name, input.valueAsNumber]));
    if (pomodoro) return values;
    const total = values.hours * 3600 + values.minutes * 60 + values.seconds;
    return total >= 1 && total <= MAX_SECONDS ? { seconds: total } : null;
  }

  function draw() {
    const value = duration ? timeText(remaining) : '—:—';
    if (display.textContent !== value) display.textContent = value;
    progress.value = duration ? 1 - remaining / duration : 0;
    document.title = state === 'running' ? `${value} · ${data.title}` : `${data.title} · Handy`;
  }

  function render(message) {
    const running = state === 'running';
    const paused = state === 'paused';
    report.dataset.phase = phase;
    report.dataset.state = state;
    heading.textContent = phases[phase].label;
    if (pomodoro && settings) rounds.textContent = `${completed} из ${settings.rounds} фокус-сессий`;
    status.textContent = message;
    fieldset.disabled = running || paused;
    start.disabled = !settings;
    start.textContent = running ? 'Пауза' : paused ? 'Продолжить' : state === 'done' ? 'Снова' : 'Старт';
    reset.disabled = state === 'ready';
    skip.disabled = running || !settings;
    draw();
  }

  function prepare(message = 'Готов к старту.') {
    clearTimeout(timeout);
    deadline = null;
    state = 'ready';
    duration = settings ? (pomodoro ? settings[phases[phase].setting] * 60 : settings.seconds) * 1000 : 0;
    remaining = duration;
    render(settings ? message : pomodoro ? 'Проверь длительности и число сессий.' : 'Укажи время от 1 секунды до 24 часов.');
  }

  function advance(finished) {
    const previous = phase;
    if (phase === 'focus') {
      if (finished) completed += 1;
      phase = completed >= settings.rounds ? 'long' : 'short';
    } else {
      if (phase === 'long') completed = 0;
      phase = 'focus';
    }
    const message = finished ? phases[previous].finished : 'Этап пропущен.';
    prepare(`${message} Дальше — ${phases[phase].label.toLowerCase()}.`);
  }

  function tick() {
    clearTimeout(timeout);
    if (deadline === null) return;
    remaining = Math.min(duration, Math.max(0, deadline - Date.now()));
    if (!remaining) {
      deadline = null;
      alarm.play();
      if (pomodoro) {
        advance(true);
      } else {
        state = 'done';
        render(phases.countdown.finished);
      }
      return;
    }
    draw();
    timeout = setTimeout(tick, remaining % 1000 || 1000);
  }

  start.addEventListener('click', () => {
    if (state === 'running') {
      tick();
      if (deadline === null) return;
      clearTimeout(timeout);
      deadline = null;
      state = 'paused';
      render('На паузе.');
      return;
    }
    if (state === 'done') remaining = duration;
    alarm.enable();
    deadline = Date.now() + remaining;
    state = 'running';
    render('Идёт отсчёт.');
    tick();
  });
  reset.addEventListener('click', () => prepare());
  skip.addEventListener('click', () => advance(false));
  sound.addEventListener('change', alarm.enable);
  fieldset.addEventListener('input', () => {
    settings = readSettings();
    if (pomodoro && settings) completed = Math.min(completed, settings.rounds);
    prepare();
  });
  document.addEventListener('visibilitychange', tick);

  fieldset.hidden = false;
  rounds.hidden = skip.hidden = !pomodoro;
  settings = readSettings();
  showReport(data);
  prepare();
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
