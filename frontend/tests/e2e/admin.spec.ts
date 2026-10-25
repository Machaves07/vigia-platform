import { authenticatedTest as test, expect } from './fixtures';
import { installerRoute, nextStep, prepare, required } from './journeys';

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
  await page.goto('/admin/users');
  await page.getByRole('button', { name: 'Invitar a una persona' }).click();
  const invite = page.getByRole('form', { name: 'Invitación', exact: true });
  await invite.getByLabel('Correo', { exact: true }).fill(`${suffix}@journey.example.test`);
  await invite.getByLabel('Nombre', { exact: true }).fill(`Cuenta sintética ${suffix}`);
  await invite.getByLabel('Rol', { exact: true }).selectOption('line_manager');
  await invite.getByLabel('Nivel', { exact: true }).selectOption('zone');
  await invite.getByLabel('Alcance', { exact: true }).selectOption(zone);
  await invite.getByRole('button', { name: 'Invitar', exact: true }).click();
  const once = page.getByRole('region', { name: 'Enlace de invitación', exact: true });
  await expect(once).toBeVisible();
  await once.getByRole('button', { name: 'Lo guardé' }).click();
  await expect(once).toHaveCount(0);
  const user = page.getByRole('row').filter({ hasText: `${suffix}@journey.example.test` });
  await user.getByRole('button', { name: /Gestionar/ }).click();
  const roles = page.getByRole('form', { name: 'Roles con alcance', exact: true });
  await roles.getByLabel('Rol', { exact: true }).selectOption('copasst');
  await roles.getByLabel('Nivel', { exact: true }).selectOption('plant');
  await roles.getByLabel('Alcance', { exact: true }).selectOption(plant);
  await roles.getByRole('button', { name: 'Asignar', exact: true }).click();
  await expect(page.getByText('Rol asignado.', { exact: true })).toBeVisible();
  await page.goto('/admin/hierarchy');
  const zoneForm = page.getByRole('form', { name: 'Crear zona', exact: true }).first();
  await zoneForm.getByLabel('Código', { exact: true }).fill(`E2E-${suffix}`);
  await zoneForm.getByLabel('Nombre', { exact: true }).fill(`Zona sintética ${suffix}`);
  const created = page.waitForResponse(
    (response) => response.url().endsWith('/zones') && response.request().method() === 'POST',
  );
  await zoneForm.getByRole('button', { name: 'Crear zona', exact: true }).click();
  expect((await created).ok()).toBe(true);
  await expect(page.getByText(`Zona sintética ${suffix}`, { exact: true })).toBeVisible();
  await page.goto(`/zones/${zone}/standards`);
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
    .getByLabel('Motivo del cambio (Obligatorio)', { exact: true })
    .fill('Declaración sintética para verificar el recorrido de catálogo');
  await nextStep(page);
  await page.getByRole('button', { name: 'Enviar', exact: true }).click();
  await expect(
    page.getByText('Se publicó una versión nueva; las versiones anteriores permanecen.', {
      exact: true,
    }),
  ).toBeVisible();
  await page.goto(`/plants/${plant}/admissions`);
  await page.getByLabel('Familia', { exact: true }).selectOption('dwell');
  await page.getByRole('radio', { name: 'No', exact: true }).check();
  await nextStep(page);
  await page.getByRole('radio', { name: 'Sí', exact: true }).check();
  await nextStep(page);
  await page.getByRole('radio', { name: 'Sí', exact: true }).check();
  await nextStep(page);
  await page.getByRole('button', { name: 'Enviar', exact: true }).click();
  await expect(page.locator('[data-failed-criterion="standard"]')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('table', { name: 'Historial de admisiones' })).toContainText(
    'Rechazada',
  );
  await page.goto('/admin/vocabulary');
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
    await expect(scope.getByText(/synthetic.png/)).toBeVisible();
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
  await page.goto(`/zones/${zone}/gates`);
  await page.getByRole('button', { name: 'Confirmar mi firma', exact: true }).click();
  await expect(page.locator('[data-part="confirmed"]')).toContainText('Firma confirmada');
});
