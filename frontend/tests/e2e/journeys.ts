import type { Page } from '@playwright/test';

import { expect } from './fixtures';

/** Las escrituras se hacen siempre desde la interfaz; solo se observa su respuesta real. */
export async function confirmFinding(page: Page, findingId: string, reason: string) {
  await page.goto(`/findings/${findingId}/classify`);
  await page.getByRole('button', { name: 'Consultar el hallazgo y su historial' }).click();
  const form = page.getByRole('form', { name: 'Registrar una clasificación' });
  await form.getByRole('radio', { name: 'confirmado', exact: true }).check();
  await form.getByLabel('Categoría del motivo').selectOption('real_coexistence');
  await form.getByLabel('Motivo de la clasificación', { exact: true }).fill(reason);
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
