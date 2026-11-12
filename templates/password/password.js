import { loadData, showError, showReport } from '../shared/report.js';

const ALPHABETS = {
  lowercase: 'abcdefghijklmnopqrstuvwxyz',
  uppercase: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
  digits: '0123456789',
  symbols: '!@#$%&*+-=?_',
};

function validLength(length) {
  return Number.isInteger(length) && length >= 4 && length <= 128;
}

function generatePassword(length, groups) {
  const alphabet = groups.join('');
  const word = new Uint32Array(1);
  const range = 2 ** 32;
  const limit = range - (range % alphabet.length);
  let characters;
  // Reject incomplete ranges and passwords that miss a selected group.
  do {
    characters = Array.from({ length }, () => {
      do {
        crypto.getRandomValues(word);
      } while (word[0] >= limit);
      return alphabet[word[0] % alphabet.length];
    });
  } while (groups.some(group => !characters.some(character => group.includes(character))));
  return characters.join('');
}

function setup(data) {
  if (!validLength(data.length)) {
    throw new Error('Укажи целую длину пароля от 4 до 128.');
  }
  if (!Array.isArray(data.groups) || !data.groups.length ||
      new Set(data.groups).size !== data.groups.length ||
      data.groups.some(group => typeof group !== 'string' || !Object.hasOwn(ALPHABETS, group))) {
    throw new Error('Выбери для groups хотя бы один набор из lowercase, uppercase, digits и symbols.');
  }
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error('Открой генератор в браузере с поддержкой Web Crypto.');
  }

  const range = document.querySelector('#password-length');
  const exact = document.querySelector('#password-length-exact');
  const options = [...document.querySelectorAll('#password-groups input')];
  const value = document.querySelector('#password-value');
  const status = document.querySelector('#password-status');
  const copyButton = document.querySelector('#password-copy');
  const regenerateButton = document.querySelector('#password-regenerate');
  range.value = exact.value = data.length;
  for (const option of options) option.checked = data.groups.includes(option.value);

  function regenerate() {
    const length = exact.valueAsNumber;
    const groups = options.filter(option => option.checked).map(option => ALPHABETS[option.value]);
    let error = '';
    if (!validLength(length)) error = 'Укажи целую длину от 4 до 128.';
    else if (!groups.length) error = 'Выбери хотя бы один набор символов.';
    value.value = error ? '' : generatePassword(length, groups);
    copyButton.disabled = regenerateButton.disabled = Boolean(error);
    status.textContent = error || 'Пароль готов.';
  }

  async function copy() {
    const password = value.value;
    if (!password) return;
    try {
      await navigator.clipboard.writeText(password);
    } catch {
      if (value.value !== password) return;
      value.focus();
      value.select();
      try {
        if (document.execCommand('copy')) {
          status.textContent = 'Пароль скопирован.';
          return;
        }
      } catch {}
      status.textContent = 'Пароль выделен. Нажми Ctrl+C или ⌘C.';
      return;
    }
    if (value.value === password) status.textContent = 'Пароль скопирован.';
  }

  range.addEventListener('input', () => {
    exact.value = range.value;
    regenerate();
  });
  exact.addEventListener('input', () => {
    if (validLength(exact.valueAsNumber)) range.value = exact.value;
    regenerate();
  });
  for (const option of options) option.addEventListener('change', regenerate);
  document.querySelector('#password-letters-digits').addEventListener('click', () => {
    for (const option of options) option.checked = option.value !== 'symbols';
    regenerate();
  });
  regenerateButton.addEventListener('click', regenerate);
  copyButton.addEventListener('click', copy);
  regenerate();
  showReport(data);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
