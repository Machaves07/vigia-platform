import { authenticatedTest as test, expect } from './fixtures';
import { installerRoute, prepare, required } from './journeys';
import { systemClock } from '../../src/state/clock';

test.use({ role: 'plant_manager' });
test.skip(({ hasTouch }) => hasTouch, 'Recorrido de escritorio del gerente.');

test('gerente: consultas del proveedor, cadena, 31 días, exposición y exportación sin clips', async ({
  authenticatedPage: page,
  roleSession,
  world,
}) => {
  test.setTimeout(360_000);
  const plant = required(world.plant_ids[0]);
  const zone = required(world.zone_ids[0]);
  const provider = await (await roleSession('provider_installer')).newPage();
  try {
    // La carga prepara por servicios internos: esta consulta HTTP produce un provider_query real.
    await installerRoute(provider, world, '/fleet');
    await expect(provider.getByRole('table').first()).toBeVisible();
  } finally {
    await provider.close();
  }
  await page.goto('/concessions');
  await page
    .getByRole('button', { name: /Ver accesos o intentos/ })
    .first()
    .click();
  await expect(
    page.getByRole('table', { name: 'Accesos o intentos del proveedor', exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('cell', { name: 'Acceso o intento', exact: true }).first(),
  ).toBeVisible();
  await page.goto('/integrity');
  await page.getByLabel('Cadena que desea verificar').selectOption(plant);
  await page.getByRole('button', { name: 'Verificar ahora' }).click();
  await expect(
    page.getByRole('status').filter({ hasText: 'Verificación solicitada' }),
  ).toBeVisible();
  const history = page.getByRole('region', { name: 'Histórico de verificación' });
  await test.step('Consultar hasta que el worker verifique la cadena', async () => {
    await expect(async () => {
      await history.getByRole('button', { name: 'Actualizar', exact: true }).click();
      await expect(
        history.getByRole('cell', { name: 'Continua', exact: true }).first(),
      ).toBeVisible();
    }).toPass({ timeout: 120_000, intervals: [2000, 5000] });
  });
  await page.goto(`/zones/${zone}/coverage`);
  const coverageResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/zones/${zone}/coverage` &&
      response.request().resourceType() === 'fetch',
    { timeout: 60_000 },
  );
  await page.getByLabel('Periodo', { exact: true }).selectOption({ label: 'Últimos 31 días' });
  const coverage = await coverageResponse;
  expect(coverage.ok()).toBe(true);
  const params = new URL(coverage.url()).searchParams;
  expect(Date.parse(required(params.get('to'))) - Date.parse(required(params.get('from')))).toBe(
    31 * 86400_000,
  );
  await expect(page.getByRole('table', { name: 'Resumen del periodo' })).toBeVisible();
  await page.goto('/metrics');
  await expect(
    page.getByRole('heading', { name: 'Métricas del programa', exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Zona', exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: 'Zona', exact: true }).selectOption(zone);
  const exposureResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/accreditation/exposure',
    { timeout: 60_000 },
  );
  await page.getByRole('button', { name: 'Consultar periodo y cobertura' }).click();
  expect((await exposureResponse).ok()).toBe(true);
  for (const name of [
    'Minutos confirmados',
    'Minutos pendientes de clasificación (no se suman)',
    'Cobertura observable del periodo (%)',
  ]) {
    await expect(
      page
        .getByRole('region', { name: 'Exposición', exact: true })
        .locator('dt')
        .filter({ hasText: name }),
    ).toBeVisible();
  }
  await expect(page.getByRole('table', { name: 'Resumen del periodo' })).toBeVisible();
  await expect(page.getByLabel(/usuario|equipo|turno/i)).toHaveCount(0);
  await page.goto('/exports');
  const hierarchy = await prepare<{ plants: { plant_id: string; zones: { zone_id: string }[] }[] }>(
    page,
    '/hierarchy',
    undefined,
    'GET',
  );
  const zoneIndex = required(
    hierarchy.plants.find((entry) => entry.plant_id === plant),
  ).zones.findIndex((entry) => entry.zone_id === zone);
  expect(zoneIndex).toBeGreaterThanOrEqual(0);
  const form = page.getByRole('form', { name: 'Solicitar exportación' });
  await form.getByRole('combobox', { name: 'Planta', exact: true }).selectOption(plant);
  await form.getByRole('checkbox').nth(zoneIndex).check();
  const now = new Date(systemClock.now());
  const from = new Date(now.getTime() - 86400_000).toISOString().slice(0, 16);
  const to = now.toISOString().slice(0, 16);
  await form.getByLabel('Desde (hora de la planta)', { exact: true }).fill(from);
  await form.getByLabel('Hasta, sin incluir (hora de la planta)', { exact: true }).fill(to);
  await form.getByLabel('Forma del paquete').selectOption('without_clips');
  const requested = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/accreditation/exports' &&
      response.request().method() === 'POST',
    { timeout: 60_000 },
  );
  await form.getByRole('button', { name: 'Solicitar exportación' }).click();
  const exportResponse = await requested;
  expect(exportResponse.ok()).toBe(true);
  const body = exportResponse.request().postDataJSON() as {
    zone_ids: string[];
    package_form: string;
  };
  expect(body.zone_ids).toEqual([zone]);
  expect(body.package_form).toBe('without_clips');
  const { export_id: exportId } = (await exportResponse.json()) as { export_id: string };
  const job = page.getByRole('region', { name: exportId, exact: true });
  await test.step('Consultar hasta que la exportación sin clips esté lista', async () => {
    await expect(async () => {
      await job.getByRole('button', { name: 'Actualizar', exact: true }).first().click();
      await expect(job.getByText('lista', { exact: true })).toBeVisible();
    }).toPass({ timeout: 120_000, intervals: [2000, 5000] });
  });
  await page.getByRole('button', { name: 'Obtener descargas de los volúmenes' }).click();
  const link = page.getByRole('link', { name: 'Descargar volumen 1', exact: true });
  await expect(link).toBeVisible();
  // Se descarga por el enlace real: no se sustituye S3 ni se sigue solo la URL de la API.
  const download = page.waitForEvent('download', { timeout: 60_000 });
  await link.click();
  const volume = await download;
  expect((await volume.failure()) === null, 'Descarga real del volumen completada').toBe(true);
  expect(await volume.path()).not.toBeNull();
});
