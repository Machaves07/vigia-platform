import type { Locator, Request } from '@playwright/test';

import { authenticatedTest as test, expect } from './fixtures';
import { expectOwnHome, required } from './journeys';

test.use({ role: 'provider_installer' });
test.skip(
  ({ hasTouch }) => hasTouch,
  'Recorrido de escritorio; las tabletas tienen sus propios recorridos.',
);

test('proveedor: solicitud, cabeceras por fase y cambio sin caché del cliente anterior', async ({
  authenticatedPage: page,
  world,
}) => {
  test.setTimeout(180_000);
  // VIG-363 (hallazgo 18): sin concesión, su inicio son sus concesiones (antes de observar).
  await expectOwnHome(page, 'Mis concesiones', '/provider/concessions');
  let active: string | undefined;
  const observed: { path: string; expected: string | undefined; actual: string | undefined }[] = [];
  // Orden de las peticiones de la lista de concesiones: el relistado tras conceder (VIG-361).
  const listings: string[] = [];
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
    if (new URL(request.url()).pathname === '/provider/concessions')
      listings.push(request.method());
  };
  page.on('request', inspect);
  // «Operar bajo» se pulsa con las lecturas del panel terminadas: al cambiar de concesión llegan
  // a la vez la sesión, la lista y las notificaciones, la página se recoloca y un clic en ese
  // intervalo no activaba la concesión en Firefox (VIG-361).
  const pending = new Set<Request>();
  page.on('request', (request) => {
    if (['fetch', 'xhr'].includes(request.resourceType())) pending.add(request);
  });
  const settle = (request: Request) => {
    pending.delete(request);
  };
  page.on('requestfinished', settle);
  page.on('requestfailed', settle);
  const operate = async (button: Locator, concession: string) => {
    await expect(button).toBeVisible();
    await expect.poll(() => pending.size, { message: 'El panel terminó sus lecturas' }).toBe(0);
    active = concession;
    await button.click();
    await expect(page.getByRole('button', { name: 'Cambiar de concesión' })).toBeVisible();
  };
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
  // Al conceder, el panel monta de nuevo la lista y la vuelve a pedir, sin cabecera. Desde el
  // segundo motor, la lista anterior ya trae una fila «Operar bajo» de esta planta: sin esperar a
  // esa petición, `active` podía fijarse antes de que saliera (VIG-361). Una vez emitida, la lista
  // anterior ya no está montada y la primera fila de la planta es la concesión nueva.
  await expect
    .poll(() => listings.slice(listings.lastIndexOf('POST') + 1).includes('GET'), {
      message: 'El panel vuelve a pedir la lista tras conceder',
    })
    .toBe(true);
  const row = page
    .getByRole('row')
    .filter({ has: page.getByText(required(world.plant_ids[0]), { exact: true }) });
  await operate(row.getByRole('button', { name: /Operar bajo/ }).first(), id);
  await expect(page.getByText(/cada consulta queda registrada/i).first()).toBeVisible();
  await page.getByRole('link', { name: /flota/i }).first().click();
  await expect(page.getByRole('table').first()).toBeVisible();
  await expect(page.locator('[data-part="concession_banner"]')).toBeVisible();
  expect(observed.filter((entry) => entry.expected === id).length).toBeGreaterThan(0);
  active = undefined;
  await page.getByRole('button', { name: 'Cambiar de concesión' }).click();
  await expect(page.getByRole('heading', { name: 'Mis concesiones' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Cambiar de concesión' })).toHaveCount(0);
  await expect(
    page.getByRole('navigation', { name: 'Navegación principal' }).getByRole('link'),
  ).toHaveCount(1);
  // Otra concesión: la misma lectura debe volver a la red, no mostrar el inventario retenido.
  await operate(
    page.getByRole('button', {
      name: `Operar bajo esta concesión: organización ${world.organization_id}`,
      exact: true,
    }),
    world.concession_id,
  );
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
  await expect(
    page.getByRole('navigation', { name: 'Navegación principal' }).getByRole('link'),
  ).toHaveCount(1);
  // Volver a la primera concesión exige de nuevo su inventario: no basta una clave distinta.
  await operate(
    page
      .getByRole('button', {
        name: `Operar bajo esta concesión: planta ${required(world.plant_ids[0])}`,
        exact: true,
      })
      .first(),
    id,
  );
  const reloaded = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/fleet/nodes' &&
      response.request().headers()['x-vigia-concession'] === id,
  );
  await page.getByRole('link', { name: /flota/i }).first().click();
  expect((await reloaded).ok()).toBe(true);
  active = undefined;
  await page.getByRole('button', { name: 'Cambiar de concesión' }).click();
  await expect(page.getByRole('heading', { name: 'Mis concesiones' })).toBeVisible();
  await expect(
    page.getByRole('navigation', { name: 'Navegación principal' }).getByRole('link'),
  ).toHaveCount(1);
  page.off('request', inspect);
  expect(observed.filter((entry) => entry.expected === undefined).length).toBeGreaterThan(0);
  expect(observed.filter((entry) => entry.actual !== entry.expected)).toEqual([]);
});
