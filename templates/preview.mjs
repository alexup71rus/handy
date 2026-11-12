import { createServer } from 'node:http';
import { readFile, realpath, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = await realpath(fileURLToPath(new URL('.', import.meta.url)));
const port = process.argv[2] ?? '4173';
if (!/^\d{1,5}$/.test(port) || Number(port) > 65535) {
  console.error('Укажи порт от 0 до 65535. Порт 0 выбирается автоматически.');
  process.exit(1);
}

const page = 'text/html; charset=utf-8';
const script = 'text/javascript; charset=utf-8';
const data = 'application/json; charset=utf-8';
const templates = new Map([
  ['chart', 'График'], ['table', 'Таблица'], ['list', 'Список'], ['clock', 'Часы'], ['dice', 'Кубик'],
  ['timer', 'Таймер и Pomodoro'],
  ['password', 'Генератор паролей'], ['toolbox', 'Локальные утилиты'], ['schedules', 'Запланированные задачи'],
]);
const assets = new Map([
  ['/shared/report.js', ['shared/report.js', script]],
  ['/shared/styles.css', ['shared/styles.css', 'text/css; charset=utf-8']],
]);
const redirects = new Map([['/', '/chart/']]);
for (const name of templates.keys()) {
  redirects.set(`/${name}`, `/${name}/`);
  assets.set(`/${name}/`, [`${name}/index.html`, page]);
  assets.set(`/${name}/index.html`, [`${name}/index.html`, page]);
  assets.set(`/${name}/${name}.js`, [`${name}/${name}.js`, script]);
  assets.set(`/${name}/data.json`, [`${name}/data.json`, data]);
}

const server = createServer(async (request, response) => {
  const activePort = server.address().port;
  const hosts = ['127.0.0.1', 'localhost'].map(host => activePort === 80 ? host : `${host}:${activePort}`);
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
  const end = (status, headers = {}) => response.writeHead(status, headers).end();
  if (!hosts.includes(request.headers.host)) return end(400);
  if (request.method !== 'GET' && request.method !== 'HEAD') return end(405, { Allow: 'GET, HEAD' });

  const path = request.url.split('?', 1)[0];
  if (redirects.has(path)) return end(302, { Location: redirects.get(path) });
  const asset = assets.get(path);
  if (!asset) return end(404);
  const [relative, type] = asset;
  const file = join(root, relative);
  try {
    if (await realpath(file) !== file) return end(404);
    const info = await stat(file);
    if (!info.isFile()) return end(404);
    if (info.size > 2 * 1024 * 1024) return end(413);
    if (request.method === 'HEAD') return end(200, { 'Content-Type': type, 'Content-Length': info.size });
    const bytes = await readFile(file);
    response.writeHead(200, { 'Content-Type': type, 'Content-Length': bytes.length }).end(bytes);
  } catch (error) {
    end(error.code === 'ENOENT' ? 404 : 500);
  }
});

server.on('error', error => {
  console.error(`Предпросмотр не запустился. ${error.message}`);
  process.exitCode = 1;
});
server.listen(Number(port), '127.0.0.1', () => {
  const origin = `http://127.0.0.1:${server.address().port}`;
  console.log([...templates].map(([name, label]) => `${label} — ${origin}/${name}/`).join('\n'));
});
