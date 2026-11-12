import { loadData, showError, showReport } from '../shared/report.js';

const validCount = count => Number.isInteger(count) && count >= 1 && count <= 20;
const validSides = sides => Number.isInteger(sides) && sides >= 2 && sides <= 1000;

function setup(data) {
  const { count = 1, sides = 6 } = data;
  if (!validCount(count) || !validSides(sides)) {
    throw new Error('Укажи от 1 до 20 кубиков в поле count и от 2 до 1 000 граней в поле sides.');
  }

  const countInput = document.querySelector('#dice-count');
  const sidesInput = document.querySelector('#dice-sides');
  const presets = document.querySelectorAll('.dice-presets button');
  const dice = document.querySelector('#dice-values');
  const summary = document.querySelector('#dice-summary');
  const shape = document.querySelector('#dice-shape');
  const rollButton = document.querySelector('#roll-again');
  countInput.value = count;
  sidesInput.value = sides;
  const word = new Uint32Array(1);
  let settings;
  let attempts = 0;

  function configure() {
    const count = countInput.valueAsNumber;
    const sides = sidesInput.valueAsNumber;
    const countOK = validCount(count);
    const sidesOK = validSides(sides);
    countInput.setAttribute('aria-invalid', String(!countOK));
    sidesInput.setAttribute('aria-invalid', String(!sidesOK));
    for (const preset of presets) {
      preset.setAttribute('aria-pressed', String(sidesOK && Number(preset.dataset.sides) === sides));
    }
    settings = countOK && sidesOK ? { count, sides } : null;
    shape.textContent = settings ? `${count}d${sides}` : '';
    shape.hidden = !settings;
    rollButton.disabled = !settings;
    rollButton.textContent = 'Бросить';
    dice.replaceChildren();
    attempts = 0;
    if (!countOK) summary.textContent = 'Укажи целое число кубиков от 1 до 20.';
    else if (!sidesOK) summary.textContent = 'Укажи целое число граней от 2 до 1 000.';
    else summary.textContent = 'Готово к броску.';
  }

  function roll() {
    if (!settings) return;
    const { count, sides } = settings;
    // Skip the incomplete group at the end of the 32-bit range.
    const limit = 2 ** 32 - (2 ** 32 % sides);
    const values = Array.from({ length: count }, () => {
      do {
        crypto.getRandomValues(word);
      } while (word[0] >= limit);
      return word[0] % sides + 1;
    });
    dice.replaceChildren(...values.map(value => {
      const face = document.createElement('li');
      face.textContent = value;
      return face;
    }));
    attempts += 1;
    const total = values.reduce((sum, value) => sum + value, 0);
    summary.textContent = count === 1 ? `Бросок ${attempts} · выпало ${total}` : `Бросок ${attempts} · сумма ${total}`;
    rollButton.textContent = 'Бросить ещё раз';
  }

  countInput.addEventListener('input', configure);
  sidesInput.addEventListener('input', configure);
  for (const preset of presets) {
    preset.addEventListener('click', () => {
      if (sidesInput.valueAsNumber === Number(preset.dataset.sides)) return;
      sidesInput.value = preset.dataset.sides;
      configure();
    });
  }
  rollButton.addEventListener('click', roll);
  configure();
  roll();
  showReport(data);
}

loadData(new URL('data.json', import.meta.url)).then(setup).catch(showError);
