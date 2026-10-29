/**
 * Memoria del cliente (NFR-APP-07) `[objetivo propio]`: el montón de la pestaña no supera 200 MB
 * con la bandeja al tamaño máximo, en Chromium sobre la construcción servida por el mundo.
 *
 * - Tamaño máximo: 200 filas (cuatro páginas de 50, PAT-APP-ESC-03: «página de 50 y máximo 200;
 *   sin virtualización hasta 200 filas»). El mundo no garantiza 200 hallazgos, así que la primera
 *   página es la respuesta real de `GET /loop/findings` y las siguientes la repiten con
 *   `finding_state_id` nuevos y un cursor sintético: la forma de cada fila es la del servidor y la
 *   interfaz la valida igual. Nada sale del navegador hacia la API con ese cursor.
 * - Medida: `performance.measureUserAgentSpecificMemory()` si la página está aislada entre
 *   orígenes; si no (la política del piloto no envía COOP/COEP), `Runtime.getHeapUsage` de CDP
 *   tras forzar la recolección. Se toma una muestra tras cada página y tras cada refresco manual
 *   (que vuelve a recorrer las cuatro páginas) y se informa el máximo.
 */
import type { CDPSession, Page, Route } from '@playwright/test';

import { authenticatedTest as test, expect } from './fixtures';

test.use({ role: 'coordinator_sst', actionTimeout: 30_000 });
test.skip(
  ({ browserName, hasTouch }) => browserName !== 'chromium' || hasTouch,
  'Medición con CDP: solo Chromium de escritorio',
);

const MB = 1024 * 1024;
const BUDGET_BYTES = 200 * MB;
const PAGE_SIZE = 50;
const MAX_ROWS = 200;
const REFRESHES = 5;
/** Cursores sintéticos de las páginas 2 a 4 (opacos para la interfaz). */
const CURSORS = ['cGVyZi1wYWdlLTI', 'cGVyZi1wYWdlLTM', 'cGVyZi1wYWdlLTQ'];

interface InboxBody {
  items: Record<string, unknown>[];
  zones: unknown[];
  partitions: unknown[];
  next_cursor: string | null;
}

/** Sirve la bandeja al tamaño máximo a partir de la primera página real. */
async function maxSizeInbox(page: Page): Promise<() => number> {
  let template: InboxBody | null = null;
  let served = 0;
  await page.route(
    (url) => url.pathname === '/loop/findings',
    async (route: Route) => {
      const url = new URL(route.request().url());
      const cursor = url.searchParams.get('cursor');
      if (cursor === null) {
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        template = (await response.json()) as InboxBody;
      }
      const base = template;
      if (base === null || base.items.length === 0) {
        throw new Error('El mundo no devolvió hallazgos para la bandeja.');
      }
      const index = cursor === null ? 0 : CURSORS.indexOf(cursor) + 1;
      expect(index).toBeGreaterThanOrEqual(0);
      const items = Array.from({ length: PAGE_SIZE }, (_, row) => ({
        ...base.items[row % base.items.length],
        finding_state_id: crypto.randomUUID(),
      }));
      served += items.length;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...base,
          items,
          next_cursor: CURSORS[index] ?? null,
        } satisfies InboxBody),
      });
    },
  );
  return () => served;
}

/** Montón de JS en uso, en bytes, tras forzar la recolección. */
async function heapBytes(page: Page, cdp: CDPSession): Promise<{ bytes: number; via: string }> {
  const isolated = await page.evaluate(() => globalThis.crossOriginIsolated);
  if (isolated) {
    const bytes = await page.evaluate(async () => {
      const result = await (
        performance as Performance & {
          measureUserAgentSpecificMemory(): Promise<{ bytes: number }>;
        }
      ).measureUserAgentSpecificMemory();
      return result.bytes;
    });
    return { bytes, via: 'measureUserAgentSpecificMemory' };
  }
  await cdp.send('HeapProfiler.collectGarbage');
  const { usedSize } = await cdp.send('Runtime.getHeapUsage');
  return { bytes: usedSize, via: 'CDP Runtime.getHeapUsage' };
}

test('NFR-APP-07: montón ≤ 200 MB con la bandeja de 200 filas', async ({
  authenticatedPage: page,
}) => {
  test.setTimeout(300_000);
  const served = await maxSizeInbox(page);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('HeapProfiler.enable');
  const samples: { step: string; bytes: number; via: string }[] = [];
  const sample = async (step: string) => {
    samples.push({ step, ...(await heapBytes(page, cdp)) });
  };

  await page.goto('/findings');
  await expect(page.getByRole('heading', { name: 'Bandeja de hallazgos' })).toBeVisible();
  const rows = page.locator('main table tbody tr');
  await expect(rows).toHaveCount(PAGE_SIZE);
  await sample('página 1');
  for (let pages = 2; pages * PAGE_SIZE <= MAX_ROWS; pages++) {
    await page.getByRole('button', { name: 'Cargar más hallazgos' }).click();
    await expect(rows).toHaveCount(pages * PAGE_SIZE);
    await sample(`página ${String(pages)}`);
  }
  await expect(page.getByRole('button', { name: 'Cargar más hallazgos' })).toHaveCount(0);

  for (let refresh = 1; refresh <= REFRESHES; refresh++) {
    const before = served();
    // Una sola actualización por página, en su cabecera (sistema visual v1); `served` confirma
    // que se volvió a pedir la bandeja.
    await page
      .locator('main > header')
      .getByRole('button', { name: 'Actualizar', exact: true })
      .click();
    await expect.poll(served).toBe(before + MAX_ROWS);
    await expect(rows).toHaveCount(MAX_ROWS);
    await sample(`refresco ${String(refresh)}`);
  }

  const max = samples.reduce((top, current) => (current.bytes > top.bytes ? current : top));
  const report = samples
    .map(({ step, bytes }) => `${step}: ${(bytes / MB).toFixed(1)} MB`)
    .join('; ');
  process.stdout.write(
    `[memoria] máximo ${(max.bytes / MB).toFixed(1)} MB (${max.step}, ${max.via}); ${report}\n`,
  );
  test.info().annotations.push({
    type: 'NFR-APP-07',
    description: `máximo ${(max.bytes / MB).toFixed(1)} MB de 200 MB (${max.via})`,
  });
  expect(max.bytes).toBeLessThanOrEqual(BUDGET_BYTES);
});
