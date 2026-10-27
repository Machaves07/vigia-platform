import type { Locator, Page } from '@playwright/test';

import { expect } from './fixtures';
import type { World } from './world';

export function required<T>(value: T | null | undefined): T {
  if (value === undefined || value === null) throw new Error('Falta un dato obligatorio del mundo');
  return value;
}

export function engineZone(world: World, project: string): string {
  const index = ['chromium', 'firefox', 'webkit'].indexOf(required(project.split('-')[0]));
  return required(world.zone_ids[index]);
}

/** Solo para preparar precondiciones: peticiones al servidor real con la sesión del navegador. */
export async function prepare<T>(
  page: Page,
  path: string,
  body?: unknown,
  method = 'POST',
  concession?: string,
): Promise<T> {
  const result = await page.evaluate(
    async ({ path, body, method, concession }) => {
      const response = await fetch(path, {
        method,
        credentials: 'same-origin',
        signal: AbortSignal.timeout(60_000),
        headers: {
          'Content-Type': 'application/json',
          ...(concession === undefined ? {} : { 'X-Vigia-Concession': concession }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      return { status: response.status, body: (await response.json()) as unknown };
    },
    { path, body, method, concession },
  );
  expect(result.status, `Precondición real: ${method} ${path}`).toBeGreaterThanOrEqual(200);
  expect(result.status, `Precondición real: ${method} ${path}`).toBeLessThan(300);
  return result.body as T;
}

/** La concesión vive solo en memoria: una entrada por URL se activa desde el panel y vuelve con Atrás. */
export async function installerRoute(page: Page, world: World, path: string): Promise<void> {
  await page.goto(path);
  await page
    .getByRole('navigation', { name: 'Navegación principal' })
    .getByRole('link', { name: 'Concesiones', exact: true })
    .click();
  await page
    .getByRole('button', {
      name: `Operar bajo esta concesión: organización ${world.organization_id}`,
      exact: true,
    })
    .click();
  await expect(page.getByRole('button', { name: 'Cambiar de concesión' })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new URL(path, world.base_url).href);
}

/** Se comprueban ambos ejes, no solo la altura CSS declarada. */
export async function touchTarget(control: Locator): Promise<void> {
  await expect(control).toBeVisible();
  const box = await control.boundingBox();
  expect(box).not.toBeNull();
  expect(required(box).width).toBeGreaterThanOrEqual(44);
  expect(required(box).height).toBeGreaterThanOrEqual(44);
}

export async function nextStep(scope: Page | Locator): Promise<void> {
  await scope.getByRole('button', { name: 'Siguiente', exact: true }).click();
}

/** Ninguna aserción de Playwright se ejecuta mientras el secreto pueda entrar en su contexto de error. */
export async function acknowledgeSecret(
  page: Page,
  kind: 'invitation_link' | 'enrollment_code',
  nodeId?: string,
): Promise<void> {
  const secret = page.locator(`[data-kind="${kind}"]`);
  let observation: {
    visible: boolean;
    valueShape: boolean;
    nodeMatches: boolean;
    fingerprints: boolean;
  } | null = null;
  let disappeared = false;
  try {
    await secret.waitFor({ state: 'visible' });
    observation = await secret.evaluate(
      (element, { kind, nodeId }) => {
        const value = element.querySelector('[data-part="value"]')?.textContent ?? '';
        const box = element.getBoundingClientRect();
        let valueShape = /^[A-Z0-9]{12}$/.test(value.trim());
        if (kind === 'invitation_link') {
          try {
            const link = new URL(value.trim(), location.origin);
            valueShape =
              link.pathname === '/invitacion' && link.hash.length > 1 && link.search === '';
          } catch {
            valueShape = false;
          }
        }
        return {
          visible: box.width > 0 && box.height > 0,
          valueShape,
          nodeMatches:
            nodeId === undefined ||
            element.querySelector('[data-part="node_id"]')?.textContent === nodeId,
          fingerprints:
            kind === 'invitation_link' ||
            [...element.querySelectorAll('[data-part="fingerprints"] code')].some((item) =>
              /^[a-f0-9]{64}$/i.test(item.textContent),
            ),
        };
      },
      { kind, nodeId },
    );
    await secret.getByRole('button', { name: 'Lo guardé', exact: true }).click();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => {
            resolve();
          });
        }),
    );
    // Se observa el resultado ANTES de la limpieza defensiva, sin copiar el valor secreto a Node.
    disappeared = (await secret.count()) === 0;
  } catch {
    throw new Error('No se pudo comprobar la entrega de una sola muestra');
  } finally {
    // Si la aplicación falla al descartarlo, no se permite que el diagnóstico lo publique.
    await secret
      .evaluateAll((elements) => {
        for (const element of elements) element.remove();
      })
      .catch(() => undefined);
  }
  expect(observation).toEqual({
    visible: true,
    valueShape: true,
    nodeMatches: true,
    fingerprints: true,
  });
  expect(disappeared, 'La aplicación descartó el secreto al confirmar').toBe(true);
}

/** Política de prueba con tres cuentas distintas, incluida representación de trabajadores. */
export async function pendingAgreement(page: Page, world: World, zoneId: string): Promise<string> {
  await prepare(
    page,
    `/plants/${required(world.plant_ids[0])}/signatory-policy`,
    {
      required_roles: ['administrator', 'plant_manager', 'copasst'],
      minimum: 3,
    },
    'PUT',
    world.concession_id,
  );
  const agreement = await prepare<{ agreement_id: string }>(
    page,
    `/zones/${zoneId}/use-agreements`,
    {
      signatories: ['administrator', 'plant_manager', 'copasst'].map((role) => ({
        role,
        user_id: world.users[role as keyof World['users']].user_id,
      })),
      document_ref: null,
      replaces_agreement_id: null,
    },
    'POST',
    world.concession_id,
  );
  return agreement.agreement_id;
}

/** La navegación principal de la aplicación: el recorrido diario no escribe direcciones. */
async function openMenu(page: Page, name: string): Promise<void> {
  await page
    .getByRole('navigation', { name: 'Navegación principal' })
    .getByRole('link', { name, exact: true })
    .click();
}

/** La bandeja completa, desde el menú: «Hallazgos» abre el panel de inicio del coordinador. */
export async function openInbox(page: Page): Promise<void> {
  await openMenu(page, 'Hallazgos');
  await page
    .getByRole('region', { name: 'Hallazgos nuevos', exact: true })
    .getByRole('link', { name: 'Abrir la bandeja completa' })
    .click();
  await expect(page.getByRole('heading', { name: 'Bandeja de hallazgos' })).toBeVisible();
}

/** Los accesos del estado del hallazgo en su detalle (VIG-359). */
export function nextSteps(page: Page): Locator {
  return page.getByRole('navigation', { name: 'Siguiente paso con este hallazgo' });
}

/**
 * Confirma el hallazgo desde la pantalla de clasificación ya abierta (se llega con clics, VIG-359).
 * Las escrituras se hacen siempre desde la interfaz; solo se observa su respuesta real.
 */
export async function confirmFinding(page: Page, findingId: string, reason: string) {
  await expect(page).toHaveURL(new RegExp(`/findings/${findingId}/classify$`));
  await page.getByRole('button', { name: 'Consultar el hallazgo y su historial' }).click();
  const form = page.getByRole('form', { name: 'Registrar una clasificación' });
  await form.getByRole('radio', { name: 'confirmado', exact: true }).check();
  await form.getByLabel('Categoría del motivo').selectOption('real_coexistence');
  await form.getByLabel('Motivo de la clasificación (Obligatorio)', { exact: true }).fill(reason);
  const written = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/loop/findings/${findingId}/classifications`) &&
      response.request().method() === 'POST',
  );
  await form.getByRole('button', { name: 'Registrar la clasificación', exact: true }).click();
  const response = await written;
  expect(response.status()).toBe(201);
  expect(await response.json()).toMatchObject({ outcome: 'confirmed', lifecycle_state: 'open' });
  const history = page.getByRole('region', { name: 'Historial de clasificaciones', exact: true });
  await expect(history.getByText(reason, { exact: true })).toBeVisible();
  await expect(page.locator('[data-role-in-use="coordinator_sst"]')).toContainText(
    'coordinador SST',
  );
  await expect(history).toContainText('con el rol coordinador SST');
}

export async function logout(page: Page) {
  // Marcadores sintéticos: la comprobación no pasa de forma vacía si se elimina la limpieza.
  await page.evaluate(() => {
    localStorage.setItem('e2e-cleanup', 'sintetico');
    sessionStorage.setItem('e2e-cleanup', 'sintetico');
  });
  await page.getByRole('button', { name: 'Cuenta', exact: true }).click();
  await page.getByRole('button', { name: 'Cerrar sesión', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(
    await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })),
  ).toEqual({ local: 0, session: 0 });
  const me = await page.context().request.get('/me');
  expect(me.status()).toBe(401);
  await page.goto('/findings');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByLabel(/correo/i)).toBeVisible();
}
