import { authenticatedTest as test, expect } from './fixtures';
import { installerRoute, pendingAgreement, required, touchTarget } from './journeys';

test.use({ role: 'copasst' });
test.skip(({ hasTouch }) => !hasTouch, 'Recorrido táctil de SCR-16.');

test('COPASST: transparencia y confirmación exclusivamente de mi firma en tableta', async ({
  authenticatedPage: page,
  roleSession,
  world,
}) => {
  expect(page.viewportSize()).toEqual({ width: 768, height: 1024 });
  expect(await page.evaluate(() => navigator.maxTouchPoints)).toBeGreaterThan(0);
  const zone = required(world.zone_ids[0]);
  const admin = await (await roleSession('provider_installer')).newPage();
  try {
    await installerRoute(admin, world, '/fleet');
    await pendingAgreement(admin, world, zone);
  } finally {
    await admin.close();
  }
  await page.goto(`/transparency/${zone}`);
  const view = page.locator('[data-part="transparency"]');
  await expect(view).toBeVisible();
  for (const name of ['Alcance declarado', 'Estándares declarados', 'Compuertas de la zona']) {
    await expect(view.getByRole('heading', { name, exact: true }).first()).toBeVisible();
  }
  await expect(view).not.toContainText(/hallazgos|personas\s*\d|vencidos|total de detecciones/i);
  await expect(page.getByRole('link', { name: /bandeja|métricas|exportación/i })).toHaveCount(0);
  const confirm = page.getByRole('button', { name: 'Confirmar mi firma', exact: true });
  await touchTarget(confirm);
  await touchTarget(page.getByRole('button', { name: 'Abrir vista en vivo', exact: true }));
  await touchTarget(page.getByRole('button', { name: 'Actualizar', exact: true }));
  const confirmation = page.waitForResponse(
    (response) =>
      response.url().includes('/confirmations') && response.request().method() === 'POST',
  );
  await confirm.tap();
  const result = await confirmation;
  expect(result.ok()).toBe(true);
  await expect(page.locator('[data-part="confirmed"]')).toContainText('Firma confirmada');
  await expect(confirm).toHaveCount(0);
  await page.reload();
  await expect(view).toBeVisible();
  await expect(confirm).toHaveCount(0);
});
