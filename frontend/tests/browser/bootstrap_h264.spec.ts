/** PR-APP-26 / VIG-343: HTTPS y canPlayType nativos, sin inyectar capacidades. */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';

let server: ViteDevServer | undefined;
let origin: string;

test.beforeAll(async () => {
  // Certificado autofirmado efímero: clave solo en memoria, nunca en el árbol ni en la salida.
  // La tubería evita /dev/stdout sobre el socket que Node usa para capturar el subproceso.
  const pem = execFileSync(
    'bash',
    [
      '-o',
      'pipefail',
      '-c',
      'openssl req -x509 -newkey rsa:2048 -nodes -keyout /dev/stdout -out /dev/stdout ' +
        '-days 1 -subj /CN=localhost 2>/dev/null | cat',
    ],
    { encoding: 'utf8' },
  );
  server = await createServer({
    configFile: false,
    root: fileURLToPath(new URL('../..', import.meta.url)),
    optimizeDeps: { noDiscovery: true, include: ['react'] },
    server: { host: '127.0.0.1', port: 0, https: { key: pem, cert: pem }, hmr: false },
    plugins: [
      {
        name: 'bootstrap-probe',
        configureServer(vite) {
          vite.middlewares.use('/probe', (_request, response) => {
            response.setHeader('Content-Type', 'text/html; charset=utf-8');
            response.end(
              '<!doctype html><html lang="es"><title>Puerta de capacidades</title>' +
                '<body><section id="vigia-unsupported" hidden>Navegador no compatible</section>' +
                '<script type="module" src="/tests/browser/probe.ts"></script></body></html>',
            );
          });
        },
      },
    ],
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (address === null || address === undefined || typeof address === 'string') {
    throw new Error('El servidor HTTPS no tiene puerto');
  }
  origin = `https://127.0.0.1:${String(address.port)}`;
});

test.afterAll(async () => {
  await server?.close();
});

test('la puerta H.264 usa el descriptor AVC nativo y falla cerrada sin decodificador', async ({
  page,
  browser,
}, testInfo) => {
  const requests: string[] = [];
  page.on('request', (request) => requests.push(request.url()));
  await page.goto(`${origin}/probe`);
  const output = page.locator('output');
  await expect(output).toBeAttached();
  // Comparar JSON estructurado, no subcadenas de la evidencia.
  const result: unknown = JSON.parse((await output.textContent()) ?? 'null');
  // eslint-disable-next-line no-console -- Evidencia real de motor y capacidades, sin datos de sesión.
  console.info(testInfo.project.name, browser.version(), JSON.stringify(result));
  const withoutH264 = testInfo.project.name === 'firefox-no-h264';
  expect(result).toMatchObject({
    secure: true,
    descriptor: 'video/mp4; codecs="avc1.42E01E"',
    native: withoutH264 ? '' : 'probably',
    explicit: withoutH264 ? '' : 'probably',
    webm: 'probably',
    missing: withoutH264 ? ['video_h264'] : [],
    loaded: !withoutH264,
    reactVersion: withoutH264 ? null : expect.stringMatching(/^19\./),
  });
  await expect(page.locator('#vigia-unsupported')).toBeVisible({ visible: withoutH264 });
  const reactRequests = requests.filter((url) => /\/react(?:\.js|\/)/.test(url));
  expect(reactRequests.length > 0).toBe(!withoutH264);
  if (testInfo.project.name === 'firefox') {
    expect(result).toMatchObject({ incomplete: '' });
  }
});
