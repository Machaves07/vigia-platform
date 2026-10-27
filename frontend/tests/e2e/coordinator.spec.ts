import { fileURLToPath } from 'node:url';

import { authenticatedTest as test, expect } from './fixtures';
import { confirmFinding, logout, nextSteps, openMenu } from './journeys';

test.use({ role: 'coordinator_sst', actionTimeout: 30_000, reuseRoleSession: false });
test.skip(({ hasTouch }) => hasTouch, 'Escritorio');

test('el coordinador recorre el lazo real con evidencia, firma y revisión humana', async ({
  authenticatedPage: page,
  browserName,
  world,
}) => {
  test.setTimeout(240_000);
  const selected = world.journeys.findings[browserName];
  expect(selected).toBeDefined();
  if (!selected) throw new Error('Faltan los hallazgos del motor en el mundo');
  process.stdout.write(
    `Perfil ${world.journeys.profile}; semilla ${String(world.journeys.seed)}\n`,
  );

  await test.step('TOTP del arnés, panel y bandeja con vencidos en cabecera', async () => {
    await page.goto('/home');
    await expect(page.getByRole('heading', { name: 'Inicio del coordinador' })).toBeVisible();
    await page.getByRole('button', { name: 'Consultar cobertura actual' }).click();
    const zones = page.getByRole('region', { name: 'Observabilidad por zona' });
    await expect(
      zones
        .getByRole('group', {
          name: /^Estado de observabilidad: (Observable|Degradada|No observable)$/,
        })
        .first(),
    ).toBeVisible();
    const nonProductive = zones.getByRole('article', { name: 'Zona no productiva' });
    await expect(nonProductive).toBeVisible();
    await expect(nonProductive.getByRole('group', { name: 'Estado de compuertas' })).toHaveText(
      'Sin acta de alcance',
    );
    await expect(
      nonProductive.getByRole('group', { name: /^Estado de observabilidad:/ }),
    ).toHaveCount(0);
    await expect(nonProductive).not.toContainText(/\d|hallazgos|personas/i);
    await page
      .getByRole('region', { name: 'Hallazgos nuevos', exact: true })
      .getByRole('link', { name: 'Abrir la bandeja completa' })
      .click();
    await expect(page.getByRole('heading', { name: 'Bandeja de hallazgos' })).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'Acciones vencidas en el alcance, sin filtros' }),
    ).toBeVisible();
    await expect(
      page
        .getByRole('region', { name: 'Acciones vencidas en el alcance, sin filtros' })
        .locator('[data-part="overdue"] strong'),
    ).toHaveText(/^\d+$/);
  });

  await test.step('detalle auditado y reproducción real, URL solo tras el clic', async () => {
    let grants = 0;
    page.on('request', (request) => {
      if (/\/evidence\/[^/]+\/read-url$/.test(request.url())) grants += 1;
    });
    await page.locator(`a[href="/findings/${selected.closure}"]`).click();
    await expect(page.getByRole('heading', { name: 'Detalle del hallazgo' })).toBeVisible();
    const play = page.getByRole('button', { name: 'Reproducir clip', exact: true }).first();
    await expect(play).toBeVisible();
    expect(grants).toBe(0);
    const grant = page.waitForResponse(/\/evidence\/[^/]+\/read-url$/);
    await play.click();
    expect((await grant).status()).toBe(200);
    expect(grants).toBe(1);
    const video = page.locator('video').first();
    await expect
      .poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime))
      .toBeGreaterThan(0);
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.error?.code ?? null),
    ).toBeNull();
    await video.evaluate((element: HTMLVideoElement) => {
      element.pause();
    });
  });

  await test.step('del detalle a clasificar y al lazo, solo con clics (VIG-359)', async () => {
    await nextSteps(page).getByRole('link', { name: 'Clasificar', exact: true }).click();
    await confirmFinding(
      page,
      selected.closure,
      'Coexistencia real observada junto al acceso de la zona sintética',
    );
    await page
      .getByRole('navigation', { name: 'Clasificación y lazo del hallazgo' })
      .getByRole('link', { name: 'Lazo de cierre del hallazgo', exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`/findings/${selected.closure}/loop$`));
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Lazo de cierre del hallazgo');
  });

  await test.step('clasificación conservada con motivo y rol efectivo', async () => {
    await openMenu(page, 'Hallazgos');
    await page.locator(`a[href="/findings/${selected.closure}"]`).click();
    const history = page.getByRole('region', { name: 'Historial completo de clasificaciones' });
    await expect(history).toContainText('Coexistencia real observada junto al acceso');
    await expect(history).toContainText('coordinador SST');
    // Ya clasificado, el detalle ofrece el lazo y reclasificar con su aviso (BR-LAZ-24).
    await expect(nextSteps(page).getByRole('link', { name: 'Lazo de cierre' })).toBeVisible();
    await expect(nextSteps(page).getByRole('link', { name: 'Reclasificar' })).toBeVisible();
    await expect(nextSteps(page).getByRole('note')).toHaveText(
      'Reclasificar como operación autorizada o falso positivo suspende el lazo mientras se resuelve.',
    );
  });

  await test.step('de la bandeja a la cola: la detección se resuelve junto a su fila', async () => {
    const reason = 'Coexistencia sintética confirmada al revisar el clip de la detección';
    await openMenu(page, 'Hallazgos');
    await page
      .locator('tbody tr')
      .filter({ has: page.locator('a[data-access="review_queue"]') })
      .first()
      .locator('td a')
      .first()
      .click();
    await expect(page.getByRole('heading', { name: 'Detalle del hallazgo' })).toBeVisible();
    await nextSteps(page)
      .getByRole('link', { name: 'Revisar en la cola de revisión', exact: true })
      .click();
    await expect(page).toHaveURL(/\/review-queue\?detection=[0-9a-f-]{36}$/);
    const target = page.locator('tbody tr[aria-current="true"]');
    await expect(target).toHaveCount(1);
    await expect(target.getByRole('rowheader')).toBeFocused();
    const resolve = target.getByRole('button', { name: 'Resolver', exact: true });
    await resolve.click();
    await expect(resolve).toHaveAttribute('aria-expanded', 'true');
    // El panel se abre en la fila siguiente a la de su detección, con el foco en su encabezado.
    const panel = page.locator('tbody tr[aria-current="true"] + tr [data-part="panel"]');
    await expect(panel.getByRole('heading', { level: 2 }).first()).toBeFocused();
    const form = panel.getByRole('form', { name: 'Registrar resolución individual' });
    await form.getByRole('radio', { name: 'confirmado en revisión', exact: true }).check();
    await form.getByLabel('Categoría del motivo').selectOption('real_coexistence');
    await form.getByLabel('Motivo de la decisión (Obligatorio)', { exact: true }).fill(reason);
    const written = page.waitForResponse(
      (response) =>
        response.url().endsWith('/resolution') && response.request().method() === 'POST',
      { timeout: 60_000 },
    );
    await form.getByRole('button', { name: 'Registrar resolución', exact: true }).click();
    expect((await written).status()).toBe(201);
    await expect(page.getByText('Resolución registrada', { exact: true })).toBeVisible();
    // De vuelta al detalle: la resolución está en el historial y el hallazgo ya tiene lazo.
    await page.goBack();
    await expect(page.getByRole('heading', { name: 'Detalle del hallazgo' })).toBeVisible();
    const resolution = page
      .getByRole('region', { name: 'Historial completo de clasificaciones' })
      .getByRole('region', { name: 'Resolución de la cola de revisión' });
    await expect(resolution).toContainText(reason);
    await expect(resolution).toContainText('confirmado en revisión');
    await expect(resolution).toContainText('coordinador SST');
    await nextSteps(page).getByRole('link', { name: 'Lazo de cierre', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Lazo de cierre del hallazgo');
  });

  await test.step('acción, PNG declarado sin personas y cierre firmado', async () => {
    // De la bandeja al lazo con el acceso de la fila (VIG-359).
    await openMenu(page, 'Hallazgos');
    await page.locator(`a[href="/findings/${selected.closure}/loop"]`).click();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Lazo de cierre del hallazgo');
    await page.getByRole('button', { name: 'Consultar el lazo', exact: true }).click();
    const action = page.getByRole('form', { name: 'Comprometer una acción sobre la zona' });
    await action.getByLabel('Tipo de acción').selectOption('engineering');
    await action
      .getByLabel('Descripción de la acción (Obligatorio)', { exact: true })
      .fill('Instalar un resguardo fijo en el acceso de la zona sintética');
    await action.getByRole('button', { name: 'Registrar la acción', exact: true }).click();
    await expect(page.getByText('Acción registrada con plazo', { exact: false })).toBeVisible();
    await page.getByRole('link', { name: 'Registrar evidencia y cierre' }).click();
    await page.getByRole('button', { name: 'Consultar el expediente de cierre' }).click();
    const evidence = page.getByRole('form', { name: 'Añadir evidencia de la acción' });
    await evidence.getByLabel('Acción cuya evidencia se registra').selectOption({
      label: 'Instalar un resguardo fijo en el acceso de la zona sintética',
    });
    await evidence
      .getByLabel('Descripción estructurada de lo ejecutado (Obligatorio)', { exact: true })
      .fill('Resguardo fijo instalado y anclado al piso del acceso de la zona');
    await evidence
      .getByLabel('Fecha y hora de ejecución en la planta')
      .fill(
        `${new Date(Date.parse(world.journeys.today) - 86_400_000).toISOString().slice(0, 10)}T08:00`,
      );
    await evidence.getByLabel('Cargo responsable de la ejecución').fill('Mantenimiento');
    await evidence
      .locator('input[type="file"]')
      .setInputFiles(fileURLToPath(new URL('./data/guard.png', import.meta.url)));
    await evidence.getByLabel('El adjunto no contiene ninguna persona identificable').check();
    await evidence.getByRole('button', { name: 'Subir', exact: true }).click();
    await expect(evidence.getByRole('listitem').filter({ hasText: 'guard.png' })).toBeVisible();
    const added = page.waitForResponse(
      (response) => response.url().endsWith('/evidence') && response.request().method() === 'POST',
    );
    await evidence.getByRole('button', { name: 'Registrar evidencia', exact: true }).click();
    const addedResponse = await added;
    expect(addedResponse.status()).toBe(201);
    const addedBody: unknown = await addedResponse.json();
    expect(addedBody).toMatchObject({ attachment_ids: [expect.any(String)] });
    await expect(
      page
        .getByRole('region', { name: 'Historial de evidencia, cierres y declaraciones' })
        .getByRole('button', { name: 'Preparar el enlace del adjunto' }),
    ).toBeVisible();
    const close = page.getByRole('region', { name: 'Cerrar y firmar', exact: true });
    await close
      .getByLabel('Descripción del cierre (Obligatorio)', { exact: true })
      .fill('Resguardo fijo verificado con la evidencia de la acción sobre la zona');
    await expect(close).toContainText('SST-SINTETICA-E2E');
    const signed = page.waitForResponse(
      (response) => response.url().endsWith('/closure') && response.request().method() === 'POST',
    );
    await close.getByRole('button', { name: 'Firmar', exact: true }).click();
    const response = await signed;
    expect(response.status()).toBe(201);
    expect(await response.json()).toMatchObject({ lifecycle_state: 'closed' });
    await expect(
      page.getByRole('region', { name: 'Historial de evidencia, cierres y declaraciones' }),
    ).toContainText('Cierre firmado');
  });

  await test.step('incerrable con política real y fecha de revisión', async () => {
    // De la bandeja a clasificar y al lazo con los accesos de la fila y de la pantalla.
    await openMenu(page, 'Hallazgos');
    await page.locator(`a[href="/findings/${selected.unclosable}/classify"]`).click();
    await confirmFinding(
      page,
      selected.unclosable,
      'Coexistencia real documentada en el acceso de la zona sintética',
    );
    await page
      .getByRole('navigation', { name: 'Clasificación y lazo del hallazgo' })
      .getByRole('link', { name: 'Lazo de cierre del hallazgo', exact: true })
      .click();
    await page.getByRole('button', { name: 'Consultar el lazo', exact: true }).click();
    await page
      .getByRole('navigation', { name: 'Siguientes registros del lazo' })
      .getByRole('link', { name: 'Declarar el hallazgo incerrable', exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`/findings/${selected.unclosable}/loop/unclosable$`));
    await page.getByRole('button', { name: 'Consultar el expediente de cierre' }).click();
    await expect(page.getByRole('region', { name: 'Política de planta' })).toContainText(
      'Criterios sintéticos de hallazgos sin alternativa segura',
    );
    const declaration = page.getByRole('region', {
      name: 'Declarar hallazgo incerrable',
      exact: true,
    });
    await declaration
      .getByLabel('Alternativa evaluada (Obligatorio)', { exact: true })
      .fill('Resguardo fijo evaluado en el acceso de la zona sintética');
    await declaration.getByRole('button', { name: 'Siguiente', exact: true }).click();
    await declaration
      .getByLabel('Motivo de descarte (Obligatorio)', { exact: true })
      .fill('La geometría del acceso no permite instalar el resguardo fijo');
    await declaration.getByRole('button', { name: 'Siguiente', exact: true }).click();
    await declaration.getByRole('button', { name: 'Siguiente', exact: true }).click();
    await declaration
      .getByLabel('Fecha de la decisión', { exact: true })
      .fill(world.journeys.today);
    await declaration.getByRole('button', { name: 'Siguiente', exact: true }).click();
    await declaration
      .getByLabel('Medida compensatoria (Obligatorio)', { exact: true })
      .fill('Delimitar el acceso con una barrera física temporal en la zona');
    await declaration.getByRole('button', { name: 'Siguiente', exact: true }).click();
    await declaration
      .getByLabel('Fecha de revisión en la planta', { exact: true })
      .fill(world.journeys.review_date);
    await declaration.getByRole('button', { name: 'Firmar', exact: true }).click();
    await declaration.getByRole('button', { name: 'Siguiente', exact: true }).click();
    const declared = page.waitForResponse(
      (response) =>
        response.url().endsWith('/unclosable') && response.request().method() === 'POST',
    );
    await declaration.getByRole('button', { name: 'Enviar', exact: true }).click();
    const response = await declared;
    expect(response.status()).toBe(201);
    expect(await response.json()).toMatchObject({ lifecycle_state: 'unclosable' });
    await expect(
      page.getByRole('region', { name: 'Historial de evidencia, cierres y declaraciones' }),
    ).toContainText('Declaración de hallazgo incerrable');
  });

  await test.step('causa raíz guardada en servidor y borrador del asistente marcado', async () => {
    await page.goto(`/findings/${selected.unclosable}/root-cause`);
    await page.getByRole('button', { name: 'Iniciar el análisis de causa raíz' }).click();
    await page
      .getByLabel('Respuesta 1', { exact: true })
      .fill('El acceso de la zona no dispone de un resguardo fijo compatible');
    await page.getByRole('button', { name: 'Guardar este porqué en el servidor' }).click();
    await expect(page.getByText('Porqué guardado en el servidor.', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Pedir un borrador al asistente' }).click();
    await expect(
      page.getByText('Borrador del asistente, requiere revisión', { exact: true }),
    ).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole('heading', { name: 'Citas de evidencia' })).toBeVisible();
    await expect(
      page.getByRole('list', { name: 'Citas de evidencia' }).getByRole('listitem').first(),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /aceptar y cerrar/i })).toHaveCount(0);
    await page
      .getByRole('button', { name: 'Copiar el borrador a la causa raíz para editarlo' })
      .click();
    await expect(
      page.getByText('Borrador copiado al paso de causa raíz: revíselo y edítelo antes de firmar.'),
    ).toBeVisible();
    await page.getByRole('button', { name: 'Guardar sin terminar', exact: true }).click();
    await expect(
      page.getByText('Progreso guardado en el servidor.', { exact: true }),
    ).toBeVisible();
    // La firma del análisis queda deliberadamente humana, separada del borrador (P8).
    await page
      .getByRole('navigation', { name: 'Navegación principal' })
      .getByRole('link', { name: 'Notificaciones', exact: true })
      .click();
    const leave = page.getByRole('button', { name: 'Salir sin guardar', exact: true });
    await expect(leave).toBeVisible();
    await leave.click();
  });

  await test.step('notificación leída y cierre de sesión sin almacenamiento residual', async () => {
    const notifications = page.getByRole('list', { name: 'Notificaciones', exact: true });
    const mark = notifications.getByRole('button', { name: 'Marcar leída', exact: true }).first();
    await expect(mark).toBeVisible();
    const read = notifications.getByText('Leída', { exact: true });
    const before = await read.count();
    const marked = page.waitForResponse(
      (response) =>
        /\/notifications\/[^/]+\/read$/.test(response.url()) &&
        response.request().method() === 'POST',
    );
    await mark.click();
    expect((await marked).status()).toBe(204);
    await expect(read).toHaveCount(before + 1);
    await logout(page);
  });
});
