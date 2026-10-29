/**
 * VIG-366 · «Español sin avisos nativos»: con el navegador en inglés (`locale: 'en-US'`, en la
 * configuración y en los contextos con sesión), ninguna pantalla con formulario deja ver un texto
 * ni un formato del navegador.
 *
 * - Todo formulario lleva `noValidate`: la burbuja nativa («Please fill out this field.») no puede
 *   aparecer.
 * - No hay campos `date`, `month` ni `datetime-local`, que pintan `mm/dd/yyyy` o «October 2026».
 * - Las palabras visibles no incluyen las de esos avisos y formatos (comparación por palabra
 *   completa, no por subcadena).
 * - Un error propio en español, accesible, sustituye a la burbuja: fecha mal escrita y campo
 *   obligatorio vacío.
 * - Los filtros por día dicen la hora de la planta, no «(UTC)».
 */
import type { Page } from '@playwright/test';

import { authenticatedTest as test, expect } from './fixtures';

test.use({ role: 'coordinator_sst' });

/** Palabras que solo aparecen si el navegador pinta sus propios avisos o formatos en inglés. */
const ENGLISH = new Set([
  'please',
  'mm/dd/yyyy',
  'january',
  'february',
  'march',
  'april',
  'june',
  'july',
  'august',
  'september',
  'october',
  'november',
  'december',
  'monday',
]);

const PAGES = ['/findings', '/findings/labels', '/zones/monthly', '/metrics', '/notifications'];

async function audit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  const report = await page.evaluate(() => ({
    forms: document.querySelectorAll('main form').length,
    withoutNoValidate: document.querySelectorAll('main form:not([novalidate])').length,
    nativeDates: document.querySelectorAll(
      'input[type="date"], input[type="month"], input[type="datetime-local"], input[type="week"], input[type="time"]',
    ).length,
    words: (document.body.innerText.toLowerCase().match(/[\p{L}/]+/gu) ?? []).filter(
      (word, index, all) => all.indexOf(word) === index,
    ),
    placeholders: Array.from(
      document.querySelectorAll<HTMLInputElement>('main input[placeholder]'),
      (input) => input.placeholder,
    ),
  }));
  expect(report.withoutNoValidate, `${path}: formularios sin noValidate`).toBe(0);
  expect(report.nativeDates, `${path}: campos de fecha nativos`).toBe(0);
  expect(
    report.words.filter((word) => ENGLISH.has(word)),
    `${path}: palabras del navegador en inglés`,
  ).toEqual([]);
  for (const placeholder of report.placeholders) {
    expect(['dd/mm/aaaa', 'hh:mm'], `${path}: formato de un campo`).toContain(placeholder);
  }
}

test('el navegador en inglés no deja ver avisos ni formatos suyos en las pantallas con formulario', async ({
  authenticatedPage: page,
}) => {
  test.setTimeout(180_000);
  for (const path of PAGES) await audit(page, path);
});

test('una fecha mal escrita y un campo vacío dan errores propios en español, accesibles', async ({
  authenticatedPage: page,
}) => {
  await page.goto('/findings/labels');
  const filters = page.getByRole('form', { name: 'Filtros de etiquetas', exact: true });
  await expect(filters).toHaveAttribute('novalidate', '');
  const from = filters.getByRole('textbox', { name: 'Desde', exact: true });
  await from.fill('31/02/2026');
  await page.getByRole('button', { name: 'Consultar etiquetas', exact: true }).click();
  // El error de formato, en español y enlazado al campo; nunca el validationMessage nativo.
  const message = page.getByText('Escriba la fecha como dd/mm/aaaa, por ejemplo 05/10/2026.', {
    exact: true,
  });
  await expect(message).toBeVisible();
  await expect(from).toHaveAttribute('aria-invalid', 'true');
  const describedBy = (await from.getAttribute('aria-describedby')) ?? '';
  const messageId = await message.evaluate((node) => node.closest('[id]')?.id ?? '');
  expect(describedBy.split(' ')).toContain(messageId);
  // «Hasta» quedó vacío: la consulta no se pide y lo dice la pantalla, no el navegador.
  await expect(
    page.getByText('Indique un periodo válido: la fecha final debe ser posterior a la inicial.', {
      exact: true,
    }),
  ).toBeVisible();
  // Con la fecha bien escrita, el campo deja de estar inválido.
  await from.fill('01/10/2026');
  await expect(message).toHaveCount(0);
});

test('los filtros por día de la bandeja dicen la hora de la planta, no UTC', async ({
  authenticatedPage: page,
}) => {
  await page.goto('/findings');
  const filters = page.getByRole('form', { name: 'Filtros de la bandeja', exact: true });
  const from = filters.getByRole('textbox', { name: /^Desde el día/ });
  await expect(from).toBeVisible();
  await expect(from).toHaveAttribute('placeholder', 'dd/mm/aaaa');
  // Cada etiqueta entera, anclada: el texto del catálogo y la zona de la planta, nunca «(UTC)».
  const day = (caption: string) =>
    filters.locator('label > span').filter({
      hasText: new RegExp(`^${caption} \\(hora de [^()]+\\)$`),
    });
  await expect(day('Desde el día')).toHaveCount(1);
  await expect(day('Hasta el día')).toHaveCount(1);
});
