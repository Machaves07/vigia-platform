import { authenticatedTest as test, expect } from './fixtures';
import { engineZone, installerRoute, prepare, required, touchTarget } from './journeys';

test.use({ role: 'provider_installer' });
test.skip(({ hasTouch }) => !hasTouch, 'Recorrido táctil de SCR-06.');

test('instalador: flota, alta, pasos, pase con clip, oclusión pendiente y enlace efímero', async ({
  authenticatedPage: page,
  world,
}, info) => {
  expect(page.viewportSize()).toEqual({ width: 768, height: 1024 });
  expect(await page.evaluate(() => navigator.maxTouchPoints)).toBeGreaterThan(0);
  await installerRoute(page, world, '/fleet');
  await expect(page.getByRole('table').first()).toBeVisible();
  await page.getByRole('link', { name: 'Declarar nodo', exact: true }).click();
  await page.getByLabel('Planta', { exact: true }).selectOption(required(world.plant_ids[0]));
  await page.getByLabel('Código del nodo', { exact: true }).fill(`E2E-${info.project.name}`);
  const declaration = page.waitForResponse(
    (response) => response.url().endsWith('/nodes') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Declarar nodo', exact: true }).click();
  const declared = await declaration;
  expect(declared.ok()).toBe(true);
  const { node_id: nodeId } = (await declared.json()) as { node_id: string };
  await page.getByRole('link', { name: /código de alta/i }).click();
  await page.getByRole('button', { name: /Emitir.*código|Solicitar.*código/i }).click();
  const secret = page.getByRole('region', { name: 'Código de alta del nodo', exact: true });
  try {
    await expect(secret).toBeVisible();
    await expect(secret).toContainText(nodeId);
    await expect(secret.getByText(/[0-9a-f]{64}/i).first()).toBeVisible();
  } finally {
    // Retirar el secreto antes de que Playwright prepare el contexto de un posible fallo.
    const acknowledge = secret.getByRole('button', { name: 'Lo guardé' });
    if ((await acknowledge.count()) > 0) await acknowledge.click();
  }
  await expect(secret).toHaveCount(0);
  const zone = engineZone(world, info.project.name);
  await installerRoute(page, world, `/zones/${zone}/commissioning`);
  const open = page.locator('[data-part="open_session"]');
  await touchTarget(open);
  await open.tap();
  await expect(page.locator('[data-part="open_step"]')).toBeVisible();
  await page
    .getByLabel('Responsable (identificador del usuario) (Obligatorio)', { exact: true })
    .fill(world.users.provider_installer.user_id);
  await touchTarget(page.locator('[data-part="open_step"]'));
  await page.locator('[data-part="open_step"]').tap();
  const step = page.locator('[data-part="step"][data-open="true"]').last();
  await expect(step).toBeVisible();
  await touchTarget(step.locator('[data-part="close_step"]'));
  await step.locator('[data-part="close_step"]').tap();
  await step.locator('[data-part="close_without_correction"]').tap();
  await expect(page.locator('[data-part="step"][data-open="true"]')).toHaveCount(0);
  await page.getByRole('button', { name: 'Abrir vista en vivo', exact: true }).tap();
  const live = page.getByRole('link', {
    name: 'Abrir la vista en vivo del nodo en una pestaña nueva',
  });
  await expect(live).toBeVisible();
  const href = new URL(required(await live.getAttribute('href')));
  // Solo se comprueba la entrega: el nodo de borde no está desplegado en este mundo.
  expect(href.protocol).toBe('https:');
  expect(href.port).toBe('8443');
  expect(href.pathname).toBe('/');
  expect(href.search).toBe('');
  expect(href.hash.startsWith('#token=')).toBe(true);
  expect(href.hash.length).toBeGreaterThan(7);
  const popup = page.context().waitForEvent('page');
  await live.tap();
  await (await popup).close();
  await expect(live).toHaveCount(0);
  await installerRoute(page, world, `/zones/${zone}/commissioning/matrix`);
  await page.locator('[data-part="select_row"]').first().tap();
  const clips = page.locator('[data-part="evidence_select"]');
  await touchTarget(clips);
  await expect(clips.locator('option')).not.toHaveCount(1);
  const clip = await clips.locator('option').nth(1).getAttribute('value');
  await clips.selectOption(required(clip));
  await touchTarget(page.locator('[data-part="register_pass"]'));
  await page.locator('[data-part="register_pass"]').tap();
  await expect(page.locator('[data-part="pass_evidence"]').last()).toHaveText(required(clip));
  await expect(clips).toHaveValue('');
  await installerRoute(page, world, `/zones/${zone}/commissioning/occlusion`);
  const catalog = await prepare<{ catalog: { cameras: { camera_id: string }[] } }>(
    page,
    `/zones/${zone}/catalog`,
    undefined,
    'GET',
    world.concession_id,
  );
  await page
    .getByLabel('Cámara ocluida (identificador) (Obligatorio)', { exact: true })
    .fill(required(catalog.catalog.cameras[0]).camera_id);
  await page.getByRole('button', { name: 'Marcar el inicio ahora' }).tap();
  await page.getByRole('button', { name: 'Marcar el fin ahora' }).tap();
  await touchTarget(page.locator('[data-part="record_occlusion"]'));
  await page.locator('[data-part="record_occlusion"]').tap();
  await expect(page.locator('[data-verification="pending"]').last()).toBeVisible();
  await expect(page.getByText(/verificando/i).first()).toBeVisible();
});
