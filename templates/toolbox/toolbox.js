import { loadData, showError, showReport } from '../shared/report.js';

const encoder = new TextEncoder();
const word = new Uint32Array(1);
const maxCount = 100;
const maxBound = 1_000_000_000;
const hex = bytes => Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');

function fromBytes(bytes) {
  try {
    const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    if (!/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(text)) return { text };
  } catch {
    // Binary input is shown as hex instead of a lossy Unicode replacement.
  }
  return { text: hex(bytes), note: 'Получились двоичные данные. Показываю байты в hex.' };
}

function base64Encode(bytes, urlsafe) {
  const text = btoa(String.fromCharCode(...bytes));
  return urlsafe ? text.replaceAll('+', '-').replaceAll('/', '_') : text;
}

function base64Decode(input, urlsafe) {
  const value = input.replace(/[ \t\r\n]/g, '');
  const alphabet = urlsafe ? /^[A-Za-z0-9_-]*={0,2}$/ : /^[A-Za-z0-9+/]*={0,2}$/;
  if (!alphabet.test(value)) throw new Error('Проверь алфавит и заполнение Base64.');
  let bytes;
  try {
    bytes = Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), char => char.charCodeAt(0));
  } catch {
    throw new Error('Проверь длину строки Base64.');
  }
  const canonical = base64Encode(bytes, urlsafe);
  if (value !== canonical && value !== canonical.replace(/=+$/, '')) {
    throw new Error('Проверь последние символы Base64.');
  }
  return fromBytes(bytes);
}

function urlDecode(input) {
  if (/%(?![0-9a-f]{2})/i.test(input)) {
    throw new Error('После каждого знака % должны идти две шестнадцатеричные цифры.');
  }
  const bytes = encoder.encode(input);
  const output = [];
  for (let index = 0; index < bytes.length; index++) {
    if (bytes[index] === 37) {
      output.push(Number.parseInt(String.fromCharCode(bytes[index + 1], bytes[index + 2]), 16));
      index += 2;
    } else {
      output.push(bytes[index]);
    }
  }
  return fromBytes(Uint8Array.from(output));
}

function jsonText(operation, input) {
  const tokens = input.match(/"(?:[^"\\]|\\.)*"|[[\]{}:,]|[^ \t\r\n"{}[\],:]+/g) ?? [];
  const opens = token => token === '[' || token === '{';
  const closes = token => token === ']' || token === '}';
  let depth = 0;
  for (const token of tokens) {
    depth += Number(opens(token)) - Number(closes(token));
    if (depth > 32) throw new Error('В JSON поддерживается до 32 уровней вложенности.');
  }
  try {
    JSON.parse(input);
  } catch {
    throw new Error('JSON содержит ошибку. Проверь кавычки, запятые и скобки.');
  }
  if (operation === 'json-validate') return 'JSON корректен.';
  if (operation === 'json-minify') return tokens.join('');
  const pieces = [];
  let previous = '';
  depth = 0;
  for (const token of tokens) {
    if (closes(token)) {
      depth -= 1;
      if (!opens(previous)) pieces.push('\n', '  '.repeat(depth));
    } else if (previous === ',' || opens(previous)) {
      pieces.push('\n', '  '.repeat(depth));
    }
    pieces.push(token === ':' ? ': ' : token);
    depth += Number(opens(token));
    previous = token;
  }
  return pieces.join('');
}

function randomIndex(size) {
  const limit = 2 ** 32 - 2 ** 32 % size;
  do {
    crypto.getRandomValues(word);
  } while (word[0] >= limit);
  return word[0] % size;
}

function randomIndexes(size, count, unique) {
  if (!unique) return Array.from({ length: count }, () => randomIndex(size));
  if (count > size) throw new Error('Для выбора без повторов нужно больше вариантов или меньше результатов.');
  const swapped = new Map();
  return Array.from({ length: count }, (_, step) => {
    const last = size - step - 1;
    const index = randomIndex(last + 1);
    const result = swapped.get(index) ?? index;
    swapped.set(index, swapped.get(last) ?? last);
    return result;
  });
}

function draw(operation, input, { min, max, count, unique }) {
  if (operation !== 'shuffle' && (!Number.isInteger(count) || count < 1 || count > maxCount)) {
    throw new Error('Укажи от 1 до 100 результатов.');
  }
  if (operation === 'numbers') {
    if (!Number.isInteger(min) || !Number.isInteger(max) || min < -maxBound || max > maxBound || min > max) {
      throw new Error('Укажи целые границы в порядке возрастания, от −1 000 000 000 до 1 000 000 000.');
    }
    return randomIndexes(max - min + 1, count, unique).map(index => min + index).join('\n');
  }
  if (operation === 'uuid') return Array.from({ length: count }, () => crypto.randomUUID()).join('\n');
  const options = operation === 'coin' ? ['Орёл', 'Решка'] :
    [...new Set(input.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];
  if (!options.length || options.length > maxCount) {
    throw new Error('Вставь от 1 до 100 разных непустых вариантов, по одному на строку.');
  }
  if (operation === 'shuffle') count = options.length;
  const distinct = operation === 'shuffle' || (operation === 'choices' && unique);
  return randomIndexes(options.length, count, distinct).map(index => options[index]).join('\n');
}

async function execute(operation, input, parameters) {
  const raw = encoder.encode(input);
  if (raw.length > 16_000) throw new Error('Вставь до 16 000 байт текста.');
  if (['numbers', 'choices', 'shuffle', 'coin', 'uuid'].includes(operation)) {
    return { text: draw(operation, input, parameters) };
  }
  if (operation.startsWith('json-')) return { text: jsonText(operation, input) };
  if (operation === 'text-count') {
    const words = input.trim() ? input.trim().split(/\s+/u).length : 0;
    const lines = input ? input.replace(/(?:\r\n|[\r\n\u2028\u2029])$/, '').split(/\r\n|[\r\n\u2028\u2029]/).length : 0;
    const stats = [
      ['Символов', [...input].length], ['Слов', words], ['Строк', lines], ['Байт UTF-8', raw.length],
    ];
    return { text: stats.map(([label, value]) => label + ' — ' + value).join('\n') };
  }
  if (operation.startsWith('base64')) {
    const urlsafe = operation.startsWith('base64url');
    return operation.endsWith('-encode') ? { text: base64Encode(raw, urlsafe) } : base64Decode(input, urlsafe);
  }
  if (operation === 'url-encode') {
    return { text: encodeURIComponent(input).replace(/[!'()*]/g, char => '%' + char.charCodeAt(0).toString(16).toUpperCase()) };
  }
  if (operation === 'url-decode') return urlDecode(input);
  if (operation === 'hex-encode') return { text: hex(raw) };
  if (operation === 'hex-decode') {
    if (!/^\s*(?:[0-9a-f]{2}\s*)*$/i.test(input)) throw new Error('Укажи пары шестнадцатеричных цифр, например 4869.');
    const pairs = input.match(/[0-9a-f]{2}/ig) ?? [];
    return fromBytes(Uint8Array.from(pairs, pair => Number.parseInt(pair, 16)));
  }
  if (operation === 'sha256') {
    if (!crypto.subtle) throw new Error('Открой сохранённый HTML в браузере с поддержкой Web Crypto.');
    return { text: hex(new Uint8Array(await crypto.subtle.digest('SHA-256', raw))) };
  }
  throw new Error('Выбери инструмент в списке.');
}

function setup(data) {
  const control = name => document.querySelector('#toolbox-' + name);
  const input = control('input');
  const output = control('output');
  const operation = control('operation');
  const status = control('status');
  const copy = control('copy');
  const unique = control('unique');
  if (Array.from(operation.options).some(option => option.value === data.operation)) {
    operation.value = data.operation;
  }
  const defaultUnique = { numbers: true, choices: true };
  let generation = 0;
  const clearResult = () => {
    generation++;
    output.value = '';
    status.textContent = '';
    status.classList.remove('toolbox-error');
    copy.disabled = true;
  };
  const configure = () => {
    const selected = operation.value;
    const options = selected === 'choices' || selected === 'shuffle';
    const noInput = ['numbers', 'coin', 'uuid'].includes(selected);
    control('min-field').hidden = control('max-field').hidden = selected !== 'numbers';
    control('settings').hidden = !['numbers', 'choices', 'coin', 'uuid'].includes(selected);
    control('unique-field').hidden = !(selected in defaultUnique);
    control('input-field').hidden = noInput;
    output.rows = noInput ? 4 : 8;
    control('input-label').textContent = options ? 'Варианты' : 'Ввод';
    control('hint').textContent = options ? 'По одному варианту на строку. Одинаковые варианты считаются одним.' :
      selected === 'numbers' ? 'Обе границы входят в диапазон.' :
      selected.startsWith('url-') ? 'Компонент URL, например значение параметра. Пробел кодируется как %20.' : '';
    input.placeholder = options ? 'Чай\nКофе\nКакао' : 'Вставь текст';
    unique.checked = defaultUnique[selected] ?? false;
    clearResult();
  };
  operation.addEventListener('change', configure);
  for (const field of [input, control('min'), control('max'), control('count'), unique]) {
    field.addEventListener('input', clearResult);
  }
  control('clear').addEventListener('click', () => {
    input.value = '';
    clearResult();
    (control('input-field').hidden ? operation : input).focus();
  });
  control('run').addEventListener('click', async () => {
    clearResult();
    const run = generation;
    try {
      const parameters = { min: control('min').valueAsNumber, max: control('max').valueAsNumber,
        count: control('count').valueAsNumber, unique: unique.checked };
      const result = await execute(operation.value, control('input-field').hidden ? '' : input.value, parameters);
      if (run !== generation) return;
      if (encoder.encode(result.text).length > 32_000) throw new Error('Результат больше 32 000 байт. Вставь меньше данных.');
      output.value = result.text;
      status.textContent = result.note ?? (result.text ? 'Готово.' : 'Получилась пустая строка.');
      copy.disabled = !result.text;
    } catch (error) {
      if (run !== generation) return;
      status.textContent = error.message;
      status.classList.add('toolbox-error');
    }
  });
  copy.addEventListener('click', async () => {
    const run = generation;
    try {
      await navigator.clipboard.writeText(output.value);
      if (run === generation) status.textContent = 'Скопировано.';
    } catch {
      if (run !== generation) return;
      output.focus();
      output.select();
      status.textContent = 'Результат выделен. Нажми Ctrl+C или ⌘C.';
    }
  });
  configure();
  showReport(data);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
