import { authenticatedTest as test, expect } from './fixtures';
import { expectOwnHome, logout } from './journeys';

test.use({ role: 'line_manager', actionTimeout: 30_000, reuseRoleSession: false });
test.skip(({ hasTouch }) => hasTouch, 'Escritorio');

test('el mando consulta su paquete y compromete la zona sin poder clasificar (A-9)', async ({
  authenticatedPage: page,
  world,
}) => {
  // VIG-363 (hallazgo 18): su inicio es su paquete mensual; nunca el panel del coordinador.
  await expectOwnHome(page, 'Paquete de revisión mensual', '/zones/monthly');
  const zone = page.getByRole('main').getByLabel('Zona', { exact: true });
  await expect(zone.locator('option[value]:not([value=""])')).toHaveCount(1);
  await zone.selectOption(world.zone_ids[0] ?? '');
  await page.getByLabel('Mes del paquete', { exact: true }).fill(world.journeys.period);
  const commitmentsRead = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/accreditation/commitments' &&
      response.request().method() === 'GET',
  );
  await page.getByRole('button', { name: 'Consultar paquete', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Hallazgos del periodo' })).toBeVisible();
  await expect(page.getByText('Estado: sellado', { exact: true })).toBeVisible();
  await expect(
    page.getByRole('button', { name: /clasificar|firmar|cerrar hallazgo|auditar/i }),
  ).toHaveCount(0);
  await expect(page.getByRole('link', { name: /clasificación|lazo|bandeja|cola/i })).toHaveCount(0);
  const commitment = `Instalar una barrera física en el acceso de la zona (${test.info().project.name})`;
  const recorded = page.getByText(commitment, { exact: true });
  const existing: unknown = await (await commitmentsRead).json();
  expect(Array.isArray(existing)).toBe(true);
  const previous = Array.isArray(existing)
    ? existing.filter(
        (item: unknown) =>
          item !== null && typeof item === 'object' && 'text' in item && item.text === commitment,
      ).length
    : 0;
  await expect(recorded).toHaveCount(previous);
  const form = page.getByRole('form', { name: 'Registrar compromiso', exact: true });
  await form.getByLabel('Compromiso sobre la zona', { exact: true }).fill(commitment);
  await form
    .getByLabel('Responsable de ejecutar el compromiso (identificador de usuario)', { exact: true })
    .fill(world.users.line_manager.user_id);
  await form.getByLabel('Plazo', { exact: true }).fill(world.journeys.review_date);
  const written = page.waitForResponse(
    (response) => response.url().endsWith('/commitments') && response.request().method() === 'POST',
  );
  await form.getByRole('button', { name: 'Registrar compromiso', exact: true }).click();
  expect((await written).status()).toBe(201);
  await expect(recorded).toHaveCount(previous + 1);
  await page.reload();
  await expect(recorded).toHaveCount(previous + 1);

  // La ocultación de la ruta no basta: forzar la operación con cookies y origen del navegador.
  const finding = world.journeys.line_finding_id;
  const rejected = await page.evaluate(async (findingId) => {
    const response = await fetch(`/loop/findings/${findingId}/classifications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        outcome: 'confirmed',
        reason_category: 'real_coexistence',
        reason_text: 'Coexistencia sintética documentada en el acceso de la zona',
        reinforced_reason: false,
      }),
    });
    return { status: response.status, body: (await response.json()) as unknown };
  }, finding);
  expect(rejected.status).toBe(404);
  expect(rejected.body).toMatchObject({ code: 'not_found' });
  await page.goto(`/findings/${finding}/classify`);
  await expect(
    page.getByRole('heading', { name: 'No disponible en su alcance.', exact: true }),
  ).toBeVisible();
  // VIG-363 (hallazgo 22): el aviso explica y lleva a lo útil del rol, su paquete mensual.
  await expect(page.locator('main [data-part="scope_explanation"]')).toHaveText(
    'Esta dirección no está disponible con su rol y su alcance, o no existe. Lo que puede consultar y registrar está en el menú y en su inicio.',
  );
  await page
    .getByRole('link', { name: 'Ir a su inicio: Paquete de revisión mensual', exact: true })
    .click();
  await expect(page).toHaveURL(/\/zones\/monthly$/);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Paquete de revisión mensual');
  await logout(page);
});
