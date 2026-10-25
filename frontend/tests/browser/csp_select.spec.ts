/**
 * VIG-345: crear un `<select>` bajo `style-src 'self'` no produce violaciones de CSP, y un estilo
 * en línea intencional sí. Sonda sintética mínima: HTML con la cabecera CSP y un único guion
 * externo; ni datos de sesión ni la aplicación. La guarda escucha el evento y la consola, igual
 * que la de VIG-299, sin filtrar ningún tipo de violación.
 */
import { createServer, type Server } from 'node:http';

import { expect, test } from '@playwright/test';

const CSP = "default-src 'self'; script-src 'self'; style-src 'self'";
const SCRIPTS = {
  select: 'document.body.appendChild(document.createElement("select"));',
  div: 'document.body.appendChild(document.createElement("div"));',
  inline:
    'const d = document.createElement("div"); d.setAttribute("style", "color: red"); ' +
    'document.body.appendChild(d);',
} as const;

let server: Server | undefined;
let origin: string;

test.beforeAll(async () => {
  server = createServer((request, response) => {
    const name = /^\/(\w+)\.js$/.exec(request.url ?? '')?.[1];
    if (name !== undefined && Object.hasOwn(SCRIPTS, name)) {
      response.writeHead(200, { 'Content-Type': 'text/javascript' });
      response.end(SCRIPTS[name as keyof typeof SCRIPTS]);
      return;
    }
    const page = /^\/page\/(\w+)$/.exec(request.url ?? '')?.[1] ?? 'div';
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': CSP,
    });
    response.end(
      `<!doctype html><html lang="es"><title>Sonda CSP</title><body>` +
        `<script src="/${page}.js"></script></body></html>`,
    );
  });
  const listening = server;
  await new Promise<void>((resolve) => listening.listen(0, '127.0.0.1', resolve));
  const address = listening.address();
  if (address === null || typeof address === 'string') throw new Error('Servidor sin puerto');
  origin = `http://127.0.0.1:${String(address.port)}`;
});

test.afterAll(async () => {
  await new Promise((resolve) => server?.close(resolve));
});

async function violations(
  page: import('@playwright/test').Page,
  script: keyof typeof SCRIPTS,
): Promise<string[]> {
  const found: string[] = [];
  page.on('console', (message) => {
    if (/Content[- ]Security[- ]Policy/i.test(message.text())) found.push('console');
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
  await page.goto(`${origin}/page/${script}`);
  await expect(page.locator(script === 'select' ? 'select' : 'div')).toBeAttached();
  // Un viaje de ida y vuelta por la misma página asegura la entrega de los eventos pendientes.
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  return found.sort();
}

test('crear un select bajo style-src self no produce violaciones', async ({ page }) => {
  expect(await violations(page, 'select')).toEqual([]);
});

test('la página de control sin select no produce violaciones', async ({ page }) => {
  expect(await violations(page, 'div')).toEqual([]);
});

test('un estilo en línea intencional sigue produciendo la violación', async ({ page }) => {
  // El aviso de consola varía por motor; el evento es obligatorio en los tres.
  expect(await violations(page, 'inline')).toContain('style-src-attr');
});
