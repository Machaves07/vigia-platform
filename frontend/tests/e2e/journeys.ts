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
