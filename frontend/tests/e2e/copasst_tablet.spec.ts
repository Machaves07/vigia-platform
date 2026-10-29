import { authenticatedTest as test, expect } from './fixtures';
import {
  auditTablet,
  expectOwnHome,
  installerRoute,
  pendingAgreement,
  required,
  roleDestinations,
  touchTarget,
} from './journeys';

test.use({ role: 'copasst' });
test.skip(({ hasTouch }) => !hasTouch, 'Recorrido táctil de SCR-16.');

test('COPASST: transparencia y confirmación exclusivamente de mi firma en tableta', async ({
  authenticatedPage: page,
  roleSession,
  world,
}, info) => {
  test.setTimeout(180_000);
  expect(page.viewportSize()).toEqual({ width: 768, height: 1024 });
  expect(info.project.use.hasTouch).toBe(true);
  // VIG-363 (hallazgo 18): su inicio es la transparencia de sus zonas.
  await expectOwnHome(page, 'Transparencia', '/transparency');
  const zone = required(world.zone_ids[0]);
  const installer = await (await roleSession('provider_installer')).newPage();
  try {
    await installerRoute(installer, world, '/fleet');
    await pendingAgreement(installer, world, zone);
  } finally {
    await installer.close();
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
  await touchTarget(
    page.getByRole('main').getByRole('button', { name: 'Actualizar', exact: true }),
  );
  const confirmation = page.waitForResponse(
    (response) =>
      response.url().includes('/confirmations') && response.request().method() === 'POST',
  );
  await confirm.tap();
  const result = await confirmation;
  expect(result.ok()).toBe(true);
  expect((await result.json()) as unknown).toMatchObject({
    user_id: world.users.copasst.user_id,
    role_in_use: 'copasst',
  });
  await expect(page.locator('[data-part="confirmed"]')).toContainText('Firma confirmada');
  await expect(confirm).toHaveCount(0);
  await page.reload();
  await expect(view).toBeVisible();
  await expect(confirm).toHaveCount(0);
});

test('COPASST en tableta: todas sus pantallas con controles de 44 px y sin desborde', async ({
  authenticatedPage: page,
  world,
}) => {
  test.setTimeout(180_000);
  const zone = required(world.zone_ids[0]);
  await page.goto('/transparency');
  // La cabecera cabe en una fila (64 px y el borde) y el menú lateral está plegado (VIG-364).
  const header = await page.getByRole('banner').boundingBox();
  expect(required(header).height).toBeLessThanOrEqual(65);
  await expect(page.getByRole('navigation', { name: 'Navegación principal' })).toBeHidden();
  const destinations = await roleDestinations(page);
  for (const path of [...destinations, `/transparency/${zone}`, '/notifications/preferences']) {
    await page.goto(path);
    await auditTablet(page, path);
  }
});
