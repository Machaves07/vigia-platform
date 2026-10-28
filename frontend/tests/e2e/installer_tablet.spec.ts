import { authenticatedTest as test, expect } from './fixtures';
import {
  acknowledgeSecret,
  auditTablet,
  engineZone,
  installerRoute,
  prepare,
  required,
  roleDestinations,
  touchTarget,
} from './journeys';

test.use({ role: 'provider_installer' });
test.skip(({ hasTouch }) => !hasTouch, 'Recorrido táctil de SCR-06.');

test('instalador: flota, alta, pasos, pase con clip, oclusión pendiente y enlace efímero', async ({
  authenticatedPage: page,
  world,
}, info) => {
  test.setTimeout(180_000);
  expect(page.viewportSize()).toEqual({ width: 768, height: 1024 });
  expect(info.project.use.hasTouch).toBe(true);
  await installerRoute(page, world, '/fleet');
  await expect(page.getByRole('table').first()).toBeVisible();
  await page.getByRole('link', { name: 'Declarar nodo', exact: true }).click();
  const declarationForm = page.getByRole('form', { name: 'Declarar nodo', exact: true });
  await expect(declarationForm).toBeVisible();
  await declarationForm
    .getByRole('combobox', { name: 'Planta', exact: true })
    .selectOption(required(world.plant_ids[0]));
  await page
    .getByLabel('Código del nodo', { exact: true })
    .fill(`E2E-${info.project.name.toUpperCase()}`);
  const declaration = page.waitForResponse(
    (response) => response.url().endsWith('/nodes') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Declarar nodo', exact: true }).click();
  const declared = await declaration;
  expect(declared.ok()).toBe(true);
  const { node_id: nodeId } = (await declared.json()) as { node_id: string };
  await page.getByRole('link', { name: /código de alta/i }).click();
  await page.getByRole('button', { name: /Emitir.*código|Solicitar.*código/i }).click();
  await acknowledgeSecret(page, 'enrollment_code', nodeId);
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
  await touchTarget(page.getByRole('button', { name: 'Abrir vista en vivo', exact: true }));
  const delivery = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/zones/${zone}/live-view-token` &&
      response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Abrir vista en vivo', exact: true }).tap();
  expect((await delivery).status(), 'Emisión real del token de vista local').toBe(200);
  const live = page.getByRole('link', {
    name: 'Abrir la vista en vivo del nodo en una pestaña nueva',
  });
  // Solo se comprueba la entrega. El token no sale del navegador ni entra en el diagnóstico.
  await live.waitFor({ state: 'visible' });
  const handoff = await live.evaluate((element) => {
    const href = new URL(element.getAttribute('href') ?? '', location.origin);
    const box = element.getBoundingClientRect();
    return {
      protocol: href.protocol,
      port: href.port,
      pathname: href.pathname,
      search: href.search,
      tokenInFragment: /^#token=[A-Za-z0-9._-]+$/.test(href.hash),
      width: box.width,
      height: box.height,
    };
  });
  let discarded = false;
  try {
    // El nodo no está en el mundo: se cancela solo la navegación, no el manejador React.
    await live.evaluate((element) => {
      element.addEventListener(
        'click',
        (event) => {
          event.preventDefault();
        },
        { once: true },
      );
    });
    await live.tap();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => {
            resolve();
          });
        }),
    );
    discarded = (await live.count()) === 0;
  } catch {
    throw new Error('No se pudo consumir el enlace efímero del nodo');
  } finally {
    await live
      .evaluateAll((elements) => {
        for (const element of elements) element.remove();
      })
      .catch(() => undefined);
  }
  expect(handoff).toMatchObject({
    protocol: 'https:',
    port: '8443',
    pathname: '/',
    search: '',
    tokenInFragment: true,
  });
  expect(handoff.width).toBeGreaterThanOrEqual(44);
  expect(handoff.height).toBeGreaterThanOrEqual(44);
  expect(discarded, 'El enlace se descartó antes de limpiar el diagnóstico').toBe(true);
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

test('instalador en tableta: todas sus pantallas con controles de 44 px y sin desborde', async ({
  authenticatedPage: page,
  world,
}, info) => {
  test.setTimeout(240_000);
  await page.goto('/provider/concessions');
  // Cabecera en una fila y menú lateral plegado (VIG-364, hallazgo 25).
  const header = await page.getByRole('banner').boundingBox();
  expect(required(header).height).toBeLessThanOrEqual(65);
  await expect(page.getByRole('navigation', { name: 'Navegación principal' })).toBeHidden();
  for (const path of await roleDestinations(page)) {
    await page.goto(path);
    await auditTablet(page, path);
  }
  // Las pantallas del cliente, bajo su concesión (vive en memoria: se activa en cada una).
  const zone = engineZone(world, info.project.name);
  for (const path of [
    '/fleet',
    '/fleet/enrollment',
    `/zones/${zone}/commissioning`,
    `/zones/${zone}/commissioning/matrix`,
    `/zones/${zone}/commissioning/occlusion`,
    `/zones/${zone}/commissioning/close`,
  ]) {
    await installerRoute(page, world, path);
    await auditTablet(page, path);
  }
});
