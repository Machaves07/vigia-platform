/**
 * TASK-574 (A-112, «Precisiones del 13-11, lectura y purga»): en un navegador real, con las
 * cabeceras de seguridad **que aplica el backend** (`SecurityHeaders` de
 * `vigia_platform.shared.api.middleware.headers`, con `img-src 'self'`), la foto del episodio se
 * pinta en el lienzo de UI-26 sin ninguna violación de CSP, y su petición lleva
 * `X-Vigia-Concession`.
 *
 * - La política no se copia aquí: se pide al propio middleware del backend (`uv run` en
 *   `backend/`) y la prueba falla si no la obtiene. Las cabeceras de la imagen y el JPEG
 *   sintético salen de `backend/openapi/u05-fixtures.json` (`episode_photos.image`), que exporta
 *   el backend y su CI comprueba con `export_u05_fixtures --check`.
 * - El servidor de la prueba responde la imagen como la plataforma: con la concesión del arnés en
 *   `X-Vigia-Concession`, los bytes; sin ella, `404 not_found` (lo que hace la cadena; sonda S2 de
 *   la revisión de vigia-platform#318).
 * - El arnés monta el componente real, construido con `vite build` (sin estilos en línea ni
 *   guiones en línea: la misma forma que la aplicación servida).
 * - Controles: una URL de objeto en un `<img>` (mutación `m_blob_url`) la bloquea la CSP real, y
 *   un `<img src>` directo (mutación `m_img_src_directo`) recibe `not_found`.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { tmpdir } from 'node:os';
import { extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';
import { build } from 'vite';

import { ALT, CONCESSION, IMAGE_PATH, PHOTO_ID } from './episode_photo/fixture';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const BACKEND = resolve(FRONTEND, '../backend');
const HARNESS = fileURLToPath(new URL('./episode_photo', import.meta.url));

/** Cabeceras de seguridad tal como el middleware del backend las pone en una respuesta. */
const BACKEND_HEADERS = [
  'import json',
  'from vigia_platform.shared.api.middleware.headers import SecurityHeaders',
  "message = {'type': 'http.response.start', 'headers': []}",
  'SecurityHeaders().apply(message)',
  "print(json.dumps([[n.decode('latin-1'), v.decode('latin-1')] for n, v in message['headers']]))",
].join('\n');

interface Fixture {
  readonly headers: Readonly<Record<string, string>>;
  readonly photo_id: string;
  readonly data_base64: string;
}

interface Seen {
  readonly path: string;
  readonly concession: string | null;
}

let server: Server | undefined;
let origin = '';
let outDir = '';
let security: readonly (readonly [string, string])[] = [];
let imageHeaders: Readonly<Record<string, string>> = {};
let imageBytes = Buffer.alloc(0);
const seen: Seen[] = [];

const TYPES: Readonly<Record<string, string>> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
};

function send(
  response: ServerResponse,
  status: number,
  headers: Readonly<Record<string, string>>,
  body: Buffer | string,
): void {
  for (const [name, value] of Object.entries(headers)) response.setHeader(name, value);
  // Las de seguridad van al final y sustituyen las de la ruta, como hace el middleware.
  for (const [name, value] of security) response.setHeader(name, value);
  response.statusCode = status;
  response.end(body);
}

function serveFile(response: ServerResponse, relative: string): void {
  const path = normalize(join(outDir, relative));
  if (!path.startsWith(`${outDir}${sep}`)) {
    send(response, 404, { 'Content-Type': 'application/json' }, '{"code":"not_found"}');
    return;
  }
  try {
    const body = readFileSync(path);
    send(
      response,
      200,
      { 'Content-Type': TYPES[extname(path)] ?? 'application/octet-stream' },
      body,
    );
  } catch {
    send(response, 404, { 'Content-Type': 'application/json' }, '{"code":"not_found"}');
  }
}

function handle(request: IncomingMessage, response: ServerResponse): void {
  const path = new URL(request.url ?? '/', 'http://127.0.0.1').pathname;
  if (path === IMAGE_PATH) {
    const concession = request.headers['x-vigia-concession'];
    const value = typeof concession === 'string' ? concession : null;
    seen.push({ path, concession: value });
    if (request.method === 'GET' && value === CONCESSION) {
      send(response, 200, imageHeaders, imageBytes);
    } else {
      send(
        response,
        404,
        { 'Content-Type': 'application/json' },
        JSON.stringify({ code: 'not_found', message_es: 'No disponible.' }),
      );
    }
    return;
  }
  if (path === '/') serveFile(response, 'index.html');
  else if (path === '/control') serveFile(response, 'control.html');
  else if (path.startsWith('/assets/')) serveFile(response, path.slice(1));
  else send(response, 404, { 'Content-Type': 'application/json' }, '{"code":"not_found"}');
}

test.beforeAll(async () => {
  const output = execFileSync('uv', ['run', '--frozen', 'python', '-c', BACKEND_HEADERS], {
    cwd: BACKEND,
    encoding: 'utf8',
  });
  security = JSON.parse(output) as [string, string][];
  const fixtures = JSON.parse(readFileSync(join(BACKEND, 'openapi/u05-fixtures.json'), 'utf8')) as {
    episode_photos: { image: Fixture };
  };
  const image = fixtures.episode_photos.image;
  expect(image.photo_id).toBe(PHOTO_ID);
  imageHeaders = image.headers;
  imageBytes = Buffer.from(image.data_base64, 'base64');

  outDir = mkdtempSync(join(tmpdir(), 'vigia-episode-photo-'));
  await build({
    configFile: false,
    logLevel: 'warn',
    root: HARNESS,
    base: '/',
    resolve: { alias: { '@': join(FRONTEND, 'src') } },
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify('episode-photo-harness'),
      'import.meta.env.VITE_CONTRACT_TAG': JSON.stringify('episode-photo-harness'),
    },
    build: {
      outDir,
      emptyOutDir: true,
      sourcemap: false,
      chunkSizeWarningLimit: 8192,
      rolldownOptions: {
        input: { index: join(HARNESS, 'index.html'), control: join(HARNESS, 'control.html') },
      },
    },
  });

  server = createServer(handle);
  const listening = server;
  await new Promise<void>((done) => listening.listen(0, '127.0.0.1', done));
  const address = listening.address();
  if (address === null || typeof address === 'string') throw new Error('Servidor sin puerto');
  origin = `http://127.0.0.1:${String(address.port)}`;
});

test.afterAll(async () => {
  await new Promise((done) => server?.close(done));
  if (outDir !== '') rmSync(outDir, { recursive: true, force: true });
});

test.beforeEach(() => {
  seen.length = 0;
});

/** Violaciones de CSP de la página: el evento y la consola, sin filtrar ningún tipo. */
async function watchViolations(page: Page): Promise<string[]> {
  const found: string[] = [];
  page.on('console', (message) => {
    if (/content.security.policy|refused to load/i.test(message.text())) found.push('console');
  });
  await page.exposeBinding('reportPolicy', (_source, directive: string) => {
    found.push(directive);
  });
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      void (globalThis as unknown as { reportPolicy(d: string): Promise<void> }).reportPolicy(
        event.effectiveDirective,
      );
    });
  });
  return found;
}

/** Un viaje de ida y vuelta por la página entrega los eventos pendientes. */
async function drain(page: Page): Promise<void> {
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(done)));
}

test('la foto se pinta en el lienzo bajo la CSP del backend, con X-Vigia-Concession', async ({
  page,
}) => {
  const violations = await watchViolations(page);
  const response = await page.goto(`${origin}/`);
  const policy = response?.headers()['content-security-policy'] ?? '';
  const directives = policy.split(';').map((directive) => directive.trim());
  expect(directives).toContain("img-src 'self'");
  expect(policy).not.toMatch(/blob:|data:|unsafe-|\*/);

  const canvas = page.getByRole('img', { name: ALT });
  await expect(canvas).toBeVisible();
  const pixels = await canvas.evaluate((element) => {
    const lienzo = element as HTMLCanvasElement;
    const context = lienzo.getContext('2d');
    if (context === null) return null;
    const { data } = context.getImageData(0, 0, lienzo.width, lienzo.height);
    let opaque = 0;
    for (let index = 3; index < data.length; index += 4) if ((data[index] ?? 0) > 0) opaque += 1;
    return { tag: lienzo.tagName, width: lienzo.width, height: lienzo.height, opaque };
  });
  expect(pixels).not.toBeNull();
  expect(pixels?.tag).toBe('CANVAS');
  expect(pixels?.width).toBeGreaterThan(0);
  expect(pixels?.height).toBeGreaterThan(0);
  // El lienzo no queda vacío: cada píxel del JPEG sintético (opaco) se dibujó.
  expect(pixels?.opaque).toBe((pixels?.width ?? 0) * (pixels?.height ?? 0));
  expect(await page.locator('img, a[download]').count()).toBe(0);
  // El clic derecho en el centro de la foto cae en la capa transparente, no en el lienzo.
  const target = await canvas.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return hit?.getAttribute('data-part') ?? hit?.tagName ?? null;
  });
  expect(target).toBe('shield');

  await drain(page);
  expect(violations).toEqual([]);
  expect(seen).toEqual([{ path: IMAGE_PATH, concession: CONCESSION }]);
});

test('control m_blob_url: una URL de objeto en un <img> la bloquea la CSP del backend', async ({
  page,
}) => {
  const violations = await watchViolations(page);
  await page.goto(`${origin}/control#blob`);
  await expect(page.locator('body')).toHaveAttribute('data-result', 'error');
  await drain(page);
  expect(violations).toContain('img-src');
  expect(seen).toEqual([{ path: IMAGE_PATH, concession: CONCESSION }]);
});

test('control m_img_src_directo: un <img src> sin la cabecera recibe not_found', async ({
  page,
}) => {
  const violations = await watchViolations(page);
  await page.goto(`${origin}/control#direct`);
  await expect(page.locator('body')).toHaveAttribute('data-result', 'error');
  await drain(page);
  expect(violations).toEqual([]);
  expect(seen).toEqual([{ path: IMAGE_PATH, concession: null }]);
});
