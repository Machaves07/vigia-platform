import type { Request } from '@playwright/test';

import { authenticatedTest as test, expect } from './fixtures';
import { required } from './journeys';

test.use({ role: 'provider_installer' });
test.skip(
  ({ hasTouch }) => hasTouch,
  'Recorrido de escritorio; las tabletas tienen sus propios recorridos.',
);

test('proveedor: solicitud, cabeceras por fase y cambio sin caché del cliente anterior', async ({
  authenticatedPage: page,
  world,
}) => {
  let active: string | undefined;
  const observed: { path: string; expected: string | undefined; actual: string | undefined }[] = [];
  const inspect = (request: Request) => {
    if (
      new URL(request.url()).origin !== world.base_url ||
      new URL(request.url()).pathname === '/version.json' ||
      !['fetch', 'xhr'].includes(request.resourceType())
    )
      return;
    observed.push({
      path: new URL(request.url()).pathname,
      expected: active,
      actual: request.headers()['x-vigia-concession'],
    });
  };
  page.on('request', inspect);
  await page.goto('/provider/concessions');
  await expect(page.getByRole('heading', { name: 'Mis concesiones' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: /principal/i }).getByRole('link')).toHaveCount(
    1,
  );
  await page.getByRole('button', { name: 'Solicitar una concesión', exact: true }).click();
  await page
    .getByLabel('Identificador de la organización cliente', { exact: true })
    .fill(world.organization_id);
  await page.getByLabel('Alcance', { exact: true }).selectOption('plant');
  await page
    .getByLabel('Identificador de la planta', { exact: true })
    .fill(required(world.plant_ids[0]));
  await page
    .getByLabel('Motivo (Obligatorio)', { exact: true })
    .fill('Comprobación sintética del recorrido del proveedor');
  await page.getByLabel('Vigencia en horas', { exact: true }).fill('1');
  await page.getByRole('button', { name: 'Ver lo que verá el cliente' }).click();
  await expect(page.getByRole('heading', { name: 'El cliente verá' })).toBeVisible();
  const granted = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/provider/concessions' &&
      response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Enviar la solicitud' }).click();
  const response = await granted;
  expect(response.ok()).toBe(true);
  const { concession_id: id } = (await response.json()) as { concession_id: string };
  const row = page
    .getByRole('row')
    .filter({ has: page.getByText(required(world.plant_ids[0]), { exact: true }) });
  await expect(row.getByRole('button', { name: /Operar bajo/ }).first()).toBeVisible();
  active = id;
  await row
    .getByRole('button', { name: /Operar bajo/ })
    .first()
    .click();
  await expect(page.getByRole('button', { name: 'Cambiar de concesión' })).toBeVisible();
  await expect(page.getByText(/cada consulta queda registrada/i).first()).toBeVisible();
  await page.getByRole('link', { name: /flota/i }).first().click();
  await expect(page.getByRole('table').first()).toBeVisible();
  await expect(page.locator('[data-part="concession_banner"]')).toBeVisible();
  expect(observed.filter((entry) => entry.expected === id).length).toBeGreaterThan(0);
  active = undefined;
  await page.getByRole('button', { name: 'Cambiar de concesión' }).click();
  await expect(page.getByRole('heading', { name: 'Mis concesiones' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Cambiar de concesión' })).toHaveCount(0);
  // Otra concesión: la misma lectura debe volver a la red, no mostrar el inventario retenido.
  active = world.concession_id;
  await page
    .getByRole('row')
    .filter({ has: page.getByText(world.organization_id, { exact: true }) })
    .getByRole('button', { name: /Operar bajo/ })
    .first()
    .click();
  const inventory = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/fleet/nodes' &&
      response.request().headers()['x-vigia-concession'] === active,
  );
  await page.getByRole('link', { name: /flota/i }).first().click();
  expect((await inventory).ok()).toBe(true);
  active = undefined;
  await page.getByRole('button', { name: 'Cambiar de concesión' }).click();
  await expect(page.getByRole('heading', { name: 'Mis concesiones' })).toBeVisible();
  page.off('request', inspect);
  expect(observed.filter((entry) => entry.expected === undefined).length).toBeGreaterThan(0);
  expect(observed.filter((entry) => entry.actual !== entry.expected)).toEqual([]);
});
