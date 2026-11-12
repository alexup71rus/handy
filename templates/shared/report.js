const number = new Intl.NumberFormat('ru-RU', { maximumSignificantDigits: 17 });
const scientific = new Intl.NumberFormat('ru-RU', { notation: 'scientific', maximumSignificantDigits: 17 });

export function formatNumber(value) {
  const magnitude = Math.abs(value);
  return (magnitude >= 1e16 || (magnitude > 0 && magnitude < 1e-6) ? scientific : number).format(value);
}

export async function loadData(url) {
  const embedded = document.querySelector('#funny-artifact-data');
  let data;
  if (embedded) {
    data = JSON.parse(embedded.textContent);
  } else {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Не удалось открыть data.json (HTTP ${response.status}).`);
    data = await response.json();
  }
  if (!data || typeof data.title !== 'string' || !data.title.trim() ||
      (data.description !== undefined && typeof data.description !== 'string')) {
    throw new Error('Проверь поля title и description в data.json.');
  }
  return data;
}

export function showReport(data) {
  document.title = `${data.title} · Handy`;
  document.querySelector('h1').textContent = data.title;
  document.querySelector('#description').textContent = data.description ?? '';
  document.querySelector('#notice').hidden = true;
  document.querySelector('#report').hidden = false;
}

export function showError(error) {
  const notice = document.querySelector('#notice');
  notice.textContent = error instanceof SyntaxError ? 'Проверь синтаксис файла data.json.' : error.message;
  notice.classList.add('error');
  notice.hidden = false;
}
