/**
 * Variante nocturna de los escenarios FS-APP (LC-APP-26, PAT-APP-RES-08) contra la plataforma
 * real del mundo de U-05, con la construcción de producción y el intermediario de fallos de U-02
 * entre el balanceador de la aplicación y la API (`faults.ts`).
 *
 * - FS-APP-01: la API deja de responder 2 minutos (`freeze`). La lectura agota sus reintentos, el
 *   circuito se abre, el aviso dice «sin conexión», el formulario de SCR-14 conserva lo escrito y
 *   ninguna escritura sale; la sonda `GET /health/live` lo cierra cuando la API vuelve.
 * - FS-APP-06: la sesión termina en el servidor a mitad del formulario. La escritura recibe
 *   `unauthenticated`, la pantalla se bloquea y, tras iniciar sesión otra vez en la misma pestaña
 *   (con el segundo factor), se vuelve a `/exports` con el borrador; la escritura no se reenvía.
 *
 * La semilla es `FC_SEED` (por omisión 452) y se registra en el informe
 * `test-results/fs-app-e2e-<semilla>-<proyecto>.json`, junto al resultado de cada escenario.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

import type { Locator, Page, Request } from '@playwright/test';
import fc from 'fast-check';

import { systemClock } from '../../src/state/clock';

import { nextTotp } from './authenticator';
import { setPlatformFault } from './faults';
import { authenticatedTest as test, expect } from './fixtures';

/** Un archivo del catálogo de textos (Node exige atributos para importar JSON). */
function texts(name: string): Readonly<Record<string, string>> {
  return JSON.parse(
    readFileSync(new URL(`../../src/texts/${name}.json`, import.meta.url), 'utf8'),
  ) as Record<string, string>;
}

/** El texto de `key`; una clave que no existe detiene la prueba en vez de buscar `undefined`. */
function catalogText(catalog: Readonly<Record<string, string>>, key: string): string {
  const value = catalog[key];
  if (value === undefined) throw new Error(`Falta la clave ${key} en el catálogo de textos.`);
  return value;
}

const appShell = texts('app_shell');
const errors = texts('errors');
const scr01 = texts('scr01');
const scr14 = texts('scr14');
const status = texts('components_status');

const SEED = (() => {
  const raw = process.env.FC_SEED;
  const seed = raw === undefined || raw.trim() === '' ? 452 : Number(raw);
  if (!Number.isSafeInteger(seed)) throw new Error('FC_SEED debe ser un entero.');
  return seed;
})();
const OUTAGE_MS = 120_000;
const WRITE = '/accreditation/exports';

interface Draft {
  readonly zones: number;
  readonly day: number;
  readonly packageForm: 'with_clips' | 'without_clips';
}

/** Parámetros del escenario con su propia semilla, derivada de la de la corrida. */
function draftFor(id: string): Draft {
  let hash = 0x811c9dc5;
  for (const char of id) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193);
  }
  const [draft] = fc.sample(
    fc.record({
      zones: fc.integer({ min: 1, max: 2 }),
      day: fc.integer({ min: 1, max: 27 }),
      packageForm: fc.constantFrom('with_clips' as const, 'without_clips' as const),
    }),
    { seed: (hash ^ SEED) | 0, numRuns: 1 },
  );
  if (draft === undefined) throw new Error(`${id}: sin parámetros.`);
  return draft;
}

interface Outcome {
  readonly id: string;
  readonly status: 'passed' | 'failed';
  readonly parameters: Draft;
}
const outcomes: Outcome[] = [];

test.afterAll(({}, testInfo) => {
  if (outcomes.length === 0) return;
  mkdirSync('test-results', { recursive: true });
  writeFileSync(
    `test-results/fs-app-e2e-${String(SEED)}-${testInfo.project.name}.json`,
    `${JSON.stringify({ report: 'fs-app-e2e', run_seed: SEED, project: testInfo.project.name, scenarios: outcomes }, null, 2)}\n`,
  );
});

/** Peticiones de la página (método y ruta, sin consulta ni cuerpo). */
function traffic(page: Page) {
  const entries: { method: string; path: string }[] = [];
  const listener = (request: Request) => {
    entries.push({ method: request.method(), path: new URL(request.url()).pathname });
  };
  page.on('request', listener);
  return {
    count: (method: string, path: string) =>
      entries.filter((entry) => entry.method === method && entry.path === path).length,
  };
}

function day(value: number): string {
  return `2026-01-${String(value).padStart(2, '0')}T08:00`;
}

/**
 * Abre SCR-14 y escribe la solicitud de exportación en la primera planta que ofrece la jerarquía
 * de la persona; devuelve el formulario y esa planta.
 */
async function openAndFill(
  page: Page,
  draft: Draft,
): Promise<{ readonly form: Locator; readonly plantOption: Locator }> {
  await page.goto('/exports');
  const form = page.getByRole('form', { name: catalogText(scr14, 'scr14.request') });
  await expect(form).toBeVisible();
  const plant = form.getByLabel(catalogText(scr14, 'scr14.plant'), { exact: true });
  const offered = plant.locator('option:not([value=""])');
  await expect(offered.first()).toBeAttached({ timeout: 30_000 });
  const plantId = (await offered.first().getAttribute('value')) ?? '';
  await plant.selectOption(plantId);
  const zones = form.getByRole('checkbox');
  await expect(zones.first()).toBeVisible();
  for (let index = 0; index < draft.zones; index += 1) await zones.nth(index).check();
  await form.getByLabel(catalogText(scr14, 'scr14.from'), { exact: true }).fill(day(draft.day));
  await form.getByLabel(catalogText(scr14, 'scr14.to'), { exact: true }).fill(day(draft.day + 1));
  await form
    .getByLabel(catalogText(scr14, 'scr14.package_form'), { exact: true })
    .selectOption(draft.packageForm);
  return { form, plantOption: plant.locator(`option[value="${plantId}"]`) };
}

async function formValues(form: Locator) {
  return {
    plant: await form.getByLabel(catalogText(scr14, 'scr14.plant'), { exact: true }).inputValue(),
    zones: await form
      .getByRole('checkbox')
      .evaluateAll((boxes) =>
        boxes.map((box) => (box as HTMLInputElement).checked).filter(Boolean),
      ),
    from: await form.getByLabel(catalogText(scr14, 'scr14.from'), { exact: true }).inputValue(),
    to: await form.getByLabel(catalogText(scr14, 'scr14.to'), { exact: true }).inputValue(),
    packageForm: await form
      .getByLabel(catalogText(scr14, 'scr14.package_form'), { exact: true })
      .inputValue(),
  };
}

async function track(id: string, parameters: Draft, run: () => Promise<void>): Promise<void> {
  try {
    await run();
    outcomes.push({ id, status: 'passed', parameters });
  } catch (error) {
    outcomes.push({ id, status: 'failed', parameters });
    throw error;
  }
}

test.describe('FS-APP con la plataforma real y el intermediario de fallos', () => {
  test.use({ role: 'coordinator_sst' });

  test('FS-APP-01 · la API no responde 2 minutos', async ({ authenticatedPage: page, world }) => {
    test.setTimeout(360_000);
    const draft = draftFor('FS-APP-01');
    await track('FS-APP-01', draft, async () => {
      const log = traffic(page);
      const { form, plantOption } = await openAndFill(page, draft);
      const written = await formValues(form);
      const notice = page.getByRole('region', {
        name: catalogText(appShell, 'app_shell.connection.label'),
      });
      await setPlatformFault(world, 'freeze');
      const outageStart = systemClock.now();
      try {
        // Una lectura explícita (actualizar la jerarquía de SCR-14) agota sus 4 intentos de 10 s.
        await page
          .getByRole('main')
          .getByRole('button', { name: catalogText(status, 'components_status.refresh.button') })
          .first()
          .click();
        await expect(notice).toHaveAttribute('data-state', 'offline', { timeout: 90_000 });
        await expect(notice.getByRole('status')).toHaveText(
          catalogText(appShell, 'app_shell.connection.offline'),
        );
        await expect(
          notice.locator('[data-text-key="app_shell.connection.writes_disabled"]'),
        ).toHaveCount(1);
        // Lectura en memoria y borrador intactos; la escritura no sale con el circuito abierto.
        await expect(plantOption).toHaveCount(1);
        expect(await formValues(form)).toEqual(written);
        await form.getByRole('button', { name: catalogText(scr14, 'scr14.request') }).click();
        await page.waitForTimeout(2_000);
        expect(log.count('POST', WRITE)).toBe(0);
        // La sonda sale en half_open (30 s después de abrir) y falla mientras dura la caída.
        await expect
          .poll(() => log.count('GET', '/health/live'), { timeout: 60_000 })
          .toBeGreaterThan(0);
        // La caída dura 2 minutos desde que se congeló la API.
        await page.waitForTimeout(Math.max(0, outageStart + OUTAGE_MS - systemClock.now()));
      } finally {
        await setPlatformFault(world, 'forward');
      }
      // Vuelve la API: la sonda cierra el circuito y el aviso desaparece.
      await expect(notice).toHaveCount(0, { timeout: 75_000 });
      expect(log.count('GET', '/health/live')).toBeGreaterThanOrEqual(2);
      expect(await formValues(form)).toEqual(written);
      const accepted = page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' && new URL(response.url()).pathname === WRITE,
      );
      await form.getByRole('button', { name: catalogText(scr14, 'scr14.request') }).click();
      expect((await accepted).status()).toBe(202);
      expect(log.count('POST', WRITE)).toBe(1);
    });
  });

  test('FS-APP-06 · la sesión vence a mitad del formulario', async ({
    authenticatedPage: page,
    world,
  }) => {
    test.setTimeout(240_000);
    const draft = draftFor('FS-APP-06');
    await track('FS-APP-06', draft, async () => {
      const log = traffic(page);
      const { form } = await openAndFill(page, draft);
      const written = await formValues(form);

      // La sesión termina en el servidor (como al vencer o al cerrarla desde otra pestaña): el
      // cierre sale del propio navegador, con su `Sec-Fetch-Site`, y la cookie deja de valer.
      const ended = await page.evaluate(async () => {
        const response = await fetch('/auth/logout', {
          method: 'POST',
          credentials: 'same-origin',
        });
        return response.status;
      });
      expect(ended).toBe(204);
      await form.getByRole('button', { name: catalogText(scr14, 'scr14.request') }).click();
      const lock = page.locator('[data-part="session_lock"]');
      await expect(lock.getByRole('alert')).toHaveText(catalogText(errors, 'errors.reauth'));
      await expect(page).toHaveURL(/\/exports$/);
      await expect(
        page.getByRole('form', { name: catalogText(scr14, 'scr14.request') }),
      ).toHaveCount(0);
      expect(log.count('POST', WRITE)).toBe(1);

      // Reautenticación en la misma pestaña, con el segundo factor de la cuenta sintética.
      const accounts = JSON.parse(readFileSync(world.credentials_file, 'utf8')) as Record<
        string,
        { readonly password: string; readonly totp_hex: string }
      >;
      const account = accounts.coordinator_sst;
      if (account === undefined) throw new Error('Sin cuenta sintética del coordinador.');
      try {
        await lock
          .getByLabel(catalogText(scr01, 'scr01.login.email'), { exact: true })
          .fill(world.users.coordinator_sst.email);
        await lock
          .getByLabel(catalogText(scr01, 'scr01.login.password'), { exact: true })
          .fill(account.password);
        await lock.getByRole('button', { name: catalogText(scr01, 'scr01.login.submit') }).click();
        await page
          .getByLabel(catalogText(scr01, 'scr01.second_factor.code'), { exact: true })
          .fill(await nextTotp(account.totp_hex, 'coordinator_sst', world.credentials_file));
        await page
          .getByRole('button', { name: catalogText(scr01, 'scr01.second_factor.submit') })
          .click();
      } catch {
        // No propagar errores de fill que puedan adjuntar la contraseña o el código.
        throw new Error('No se pudo reautenticar por la interfaz con segundo factor');
      }

      // Misma ruta, mismo borrador; la escritura que recibió `unauthenticated` no se reenvió.
      await expect(page).toHaveURL(/\/exports$/, { timeout: 30_000 });
      const restored = page.getByRole('form', { name: catalogText(scr14, 'scr14.request') });
      await expect(restored).toBeVisible();
      await expect.poll(() => formValues(restored)).toEqual(written);
      await page.waitForTimeout(3_000);
      expect(log.count('POST', WRITE)).toBe(1);
      const accepted = page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' && new URL(response.url()).pathname === WRITE,
      );
      await restored.getByRole('button', { name: catalogText(scr14, 'scr14.request') }).click();
      expect((await accepted).status()).toBe(202);
      expect(log.count('POST', WRITE)).toBe(2);
    });
  });
});
