import { authenticatedTest as test, expect } from './fixtures';
import {
  acknowledgeSecret,
  expectNoPageOverflow,
  expectOneRefresh,
  expectOwnHome,
  installerRoute,
  nextStep,
  openMenu,
  openMenuWith,
  prepare,
  required,
} from './journeys';

test.use({ role: 'administrator' });
test.skip(({ hasTouch }) => hasTouch, 'Recorrido de escritorio del administrador.');

test('administrador: usuarios, jerarquía, catálogo, rechazo registrado, vocabulario y acuerdos', async ({
  authenticatedPage: page,
  roleSession,
  world,
}, info) => {
  test.setTimeout(180_000);
  const suffix = info.project.name;
  const zone = required(world.zone_ids.at(-1));
  const plant = required(world.plant_ids[0]);
  // VIG-363 (hallazgos 18 y 19): su inicio propio y cada pantalla solo con clics desde el menú,
  // cuya entrada tiene el nombre de la página que abre.
  await expectOwnHome(page, 'Inicio', '/fleet');
  // VIG-365: «Pendiente de su parte» con sus tres pendientes, las áreas y una sola actualización.
  const pendingWork = page.getByRole('region', { name: 'Pendiente de su parte', exact: true });
  await expect(pendingWork.locator('[data-pending]')).toHaveCount(3);
  await expect(
    page.getByRole('region', { name: 'Áreas de configuración', exact: true }).locator('li'),
  ).toHaveCount(8);
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  await expectOneRefresh(page);
  await expectNoPageOverflow(page);
  await openMenu(page, 'Usuarios y roles');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Usuarios');
  await page.getByRole('button', { name: 'Invitar a una persona' }).click();
  const invite = page.getByRole('form', { name: 'Invitación', exact: true });
  await invite.getByLabel('Correo', { exact: true }).fill(`${suffix}@journey.example.test`);
  await invite.getByLabel('Nombre', { exact: true }).fill(`Cuenta sintética ${suffix}`);
  await invite.getByLabel('Rol', { exact: true }).selectOption('line_manager');
  await invite.getByLabel('Nivel', { exact: true }).selectOption('zone');
  await invite.getByLabel('Alcance', { exact: true }).selectOption(zone);
  await invite.getByRole('button', { name: 'Invitar', exact: true }).click();
  await acknowledgeSecret(page, 'invitation_link');
  const user = page.getByRole('row').filter({ hasText: `${suffix}@journey.example.test` });
  await user.getByRole('button', { name: /Gestionar/ }).click();
  const roles = page.getByRole('form', { name: 'Roles con alcance', exact: true });
  await roles.getByLabel('Rol', { exact: true }).selectOption('copasst');
  await roles.getByLabel('Nivel', { exact: true }).selectOption('plant');
  await roles.getByLabel('Alcance', { exact: true }).selectOption(plant);
  await roles.getByRole('button', { name: 'Asignar', exact: true }).click();
  await expect(page.getByText('Rol asignado.', { exact: true })).toBeVisible();
  await openMenu(page, 'Plantas y zonas');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Plantas y zonas');
  const zoneForm = page.getByRole('form', { name: 'Crear zona', exact: true }).first();
  await zoneForm.getByLabel('Código', { exact: true }).fill(`E2E-${suffix.toUpperCase()}`);
  await zoneForm.getByLabel('Nombre', { exact: true }).fill(`Zona sintética ${suffix}`);
  const created = page.waitForResponse(
    (response) => response.url().endsWith('/zones') && response.request().method() === 'POST',
  );
  await zoneForm.getByRole('button', { name: 'Crear zona', exact: true }).click();
  expect((await created).ok()).toBe(true);
  await expect(
    page.getByText(`E2E-${suffix.toUpperCase()} — Zona sintética ${suffix}`, { exact: true }),
  ).toBeVisible();
  await openMenuWith(page, 'Catálogo de estándares', zone);
  await expect(page).toHaveURL(new RegExp(`/zones/${zone}/catalog$`));
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Catálogo de estándares');
  await page.getByRole('button', { name: 'Declarar estándar', exact: true }).click();
  await page.getByLabel('Familia admitida', { exact: true }).selectOption('coexistence');
  await page
    .getByLabel('Título del estándar', { exact: true })
    .fill(`Estándar sintético ${suffix}`);
  await page
    .getByLabel('Texto declarado', { exact: true })
    .fill('La zona conserva separación de la operación activa.');
  await nextStep(page);
  await page.getByRole('button', { name: 'Usar plantilla de esta familia' }).click();
  await expect(page.getByLabel('Predicado', { exact: true })).not.toHaveValue('');
  await nextStep(page);
  await page
    .getByRole('region', { name: 'Declarar estándar', exact: true })
    .getByLabel('Motivo del cambio (Obligatorio)', { exact: true })
    .fill('Declaración sintética para verificar el recorrido de catálogo');
  await nextStep(page);
  await page.getByRole('button', { name: 'Enviar', exact: true }).click();
  await expect(
    page.getByText('Se publicó una versión nueva; las versiones anteriores permanecen.', {
      exact: true,
    }),
  ).toBeVisible();
  await openMenuWith(page, 'Admisión de familias', plant);
  await expect(page).toHaveURL(new RegExp(`/plants/${plant}/admissions$`));
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Admisión de familias');
  await page.getByRole('combobox', { name: 'Familia', exact: true }).selectOption('dwell');
  await page.getByRole('radio', { name: 'No', exact: true }).check();
  await nextStep(page);
  await page.getByRole('radio', { name: 'Sí', exact: true }).check();
  await nextStep(page);
  await page.getByRole('radio', { name: 'Sí', exact: true }).check();
  await nextStep(page);
  await page.getByRole('button', { name: 'Enviar', exact: true }).click();
  await expect(page.locator('[data-failed-criterion="standard"]')).toBeVisible();
  await page.reload();
  await expect(
    page
      .getByRole('table', { name: 'Historial de admisiones' })
      .getByRole('cell', { name: 'Rechazada', exact: true })
      // El mundo es compartido: cada motor deja la suya.
      .first(),
  ).toBeVisible();
  await openMenu(page, 'Vocabulario bloqueado');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    'Vocabulario bloqueado de la organización',
  );
  await page
    .getByLabel('Término', { exact: true })
    .fill(`garantiasintetica${suffix.replaceAll('-', '')}`);
  await page.getByLabel('Alternativa sugerida', { exact: true }).fill('Observación de la zona');
  await page
    .getByLabel('Motivo del cambio de vocabulario (Obligatorio)', { exact: true })
    .fill('Configuración sintética del vocabulario de la organización');
  await page.getByRole('button', { name: 'Registrar cambio de vocabulario', exact: true }).click();
  await expect(
    page.getByRole('region', { name: 'Registros de cambios confirmados en esta visita' }),
  ).toContainText('Registro creado');
  // El acta la registra commissioning.run (instalador); el administrador confirma solo su firma.
  const installer = await (await roleSession('provider_installer')).newPage();
  const uploadStatuses: number[] = [];
  installer.on('response', (response) => {
    if (
      response.request().method() === 'PUT' &&
      new URL(response.url()).origin === world.store_origin
    )
      uploadStatuses.push(response.status());
  });
  try {
    await installerRoute(installer, world, '/fleet');
    await prepare(
      installer,
      `/plants/${plant}/signatory-policy`,
      {
        required_roles: ['administrator', 'plant_manager', 'copasst'],
        minimum: 3,
      },
      'PUT',
      world.concession_id,
    );
    await installerRoute(installer, world, `/zones/${zone}/gates`);
    const scope = installer.locator('[data-part="scope_record"]');
    await scope
      .getByLabel('Texto del alcance de la zona')
      .fill('Alcance sintético de seguridad de la zona industrial.');
    await nextStep(scope);
    for (const camera of await scope
      .getByRole('checkbox', { name: /Incluir en el acta la cámara/ })
      .all()) {
      await camera.check();
    }
    for (const framing of await scope.getByLabel(/Descripción del encuadre de la cámara/).all()) {
      await framing.fill('Encuadre sintético del área de seguridad');
    }
    await nextStep(scope);
    await scope
      .getByRole('checkbox', { name: 'Declaro que verifiqué el difuminado en la captura adjunta' })
      .check();
    // PNG de un píxel generado, sin fotografía ni datos personales.
    await scope.getByLabel('Captura difuminada', { exact: true }).setInputFiles({
      name: 'synthetic.png',
      mimeType: 'image/png',
      buffer: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aHzQAAAAASUVORK5CYII=',
        'base64',
      ),
    });
    await scope.getByRole('button', { name: 'Subir', exact: true }).click();
    try {
      await expect(scope.getByText(/synthetic.png/)).toBeVisible();
    } catch {
      throw new Error(
        `Falló la subida PNG al almacén (HTTP ${uploadStatuses.join(',') || 'sin respuesta'})`,
      );
    }
    await nextStep(scope);
    await scope.getByLabel('Documento firmado del acta de alcance', { exact: true }).setInputFiles({
      name: 'scope.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4\n% Documento sintético de prueba\n%%EOF'),
    });
    await scope.getByRole('button', { name: 'Subir', exact: true }).click();
    await expect(scope.getByText(/scope.pdf/)).toBeVisible();
    await nextStep(scope);
    await scope.getByRole('button', { name: 'Enviar', exact: true }).click();
    await expect(scope.locator('[data-part="scope_recorded"]')).toBeVisible();
    const agreement = installer.locator('[data-part="agreement_form"]');
    // La política inicial puede haberse leído antes de modificarla: se eligen los roles explícitos.
    for (const [index, role] of ['administrator', 'plant_manager', 'copasst'].entries()) {
      await agreement
        .getByLabel(`Rol del firmante ${String(index + 1)}`, { exact: true })
        .selectOption(role);
      await agreement
        .getByLabel(`Identificador de usuario del firmante ${String(index + 1)}`, { exact: true })
        .fill(world.users[role as keyof typeof world.users].user_id);
    }
    await agreement.getByRole('button', { name: 'Registrar el acuerdo', exact: true }).click();
    await expect(installer.locator('[data-part="created_agreement"]')).toBeVisible();
  } finally {
    await installer.close();
  }
  await openMenuWith(page, 'Compuertas y acuerdos por zona', zone);
  await expect(page).toHaveURL(new RegExp(`/zones/${zone}/gates$`));
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Actas, acuerdos y política');
  const confirmation = page.waitForResponse(
    (response) =>
      response.url().includes('/confirmations') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Confirmar mi firma', exact: true }).click();
  const confirmed = await confirmation;
  expect(confirmed.ok()).toBe(true);
  expect((await confirmed.json()) as unknown).toMatchObject({
    user_id: world.users.administrator.user_id,
    role_in_use: 'administrator',
  });
  await expect(page.locator('[data-part="confirmed"]')).toContainText('Firma confirmada');
});
