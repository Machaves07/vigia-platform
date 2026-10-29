import type { Locator, Page } from '@playwright/test';

import { expect } from './fixtures';
import type { World } from './world';

export function required<T>(value: T | null | undefined): T {
  if (value === undefined || value === null) throw new Error('Falta un dato obligatorio del mundo');
  return value;
}

/**
 * Escribe un momento `aaaa-mm-ddThh:mm` como lo escribe la persona (VIG-366, sin
 * `datetime-local`): la fecha `dd/mm/aaaa` en el campo que lleva la etiqueta y la hora `hh:mm` en
 * el suyo, «Hora (hh:mm, 24 horas)», dentro del mismo grupo.
 */
export async function fillDateTime(date: Locator, value: string): Promise<void> {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})$/.exec(value);
  if (match === null) throw new Error(`Momento no válido: ${value}`);
  const [, year, month, day, time] = match;
  await date.fill(`${day ?? ''}/${month ?? ''}/${year ?? ''}`);
  await date
    .locator('xpath=ancestor::span[@data-part="date_time_input"][1]')
    .getByRole('textbox', { name: 'Hora (hh:mm, 24 horas)', exact: true })
    .fill(time ?? '');
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
  // Sin concesión, el único destino del instalador es su inicio: «Mis concesiones».
  await openMenu(page, 'Inicio');
  await expect(page).toHaveURL(/\/provider\/concessions$/);
  await page
    .getByRole('button', {
      name: `Operar bajo esta concesión: Organización ${world.organization_id}`,
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

/**
 * Tableta (VIG-364, hallazgo 25): en la pantalla abierta, ningún control interactivo visible mide
 * menos de 44 × 44 px (`touchTarget` sobre todos a la vez) y la página no desborda en horizontal
 * (`scrollWidth <= clientWidth`). Espera a que ninguna sección siga cargando.
 */
export async function auditTablet(page: Page, where: string): Promise<void> {
  await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  const report = await page.evaluate(() => {
    const selector = [
      'a[href]',
      'button',
      'input:not([type="hidden"])',
      'select',
      'textarea',
      'summary',
      '[role="button"]',
      '[tabindex]:not([tabindex="-1"])',
    ].join(',');
    const small: string[] = [];
    for (const element of document.querySelectorAll<HTMLElement>(selector)) {
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      if (box.width === 0 || box.height === 0 || style.visibility === 'hidden') continue;
      if (box.width >= 44 && box.height >= 44) continue;
      const part = element.getAttribute('data-part') ?? element.getAttribute('name') ?? '';
      const caption = (element.getAttribute('aria-label') ?? element.textContent)
        .trim()
        .slice(0, 40);
      small.push(
        `${element.tagName.toLowerCase()}[${part}] «${caption}» ${String(Math.round(box.width))}×${String(Math.round(box.height))}`,
      );
    }
    const root = document.documentElement;
    return { small, scrollWidth: root.scrollWidth, clientWidth: root.clientWidth };
  });
  expect(report.small, `${where}: controles por debajo de 44 × 44 px`).toEqual([]);
  expect(
    report.scrollWidth,
    `${where}: desborde horizontal (${String(report.scrollWidth)} > ${String(report.clientWidth)})`,
  ).toBeLessThanOrEqual(report.clientWidth);
}

/**
 * Las direcciones a las que el rol llega desde el armazón en la página abierta: barra inferior,
 * menú completo (desplegado con «Menú»), campana y cuenta. Sin repetir, en orden de aparición.
 */
export async function roleDestinations(page: Page): Promise<string[]> {
  const hrefs = (scope: Locator) =>
    scope
      .locator('a[href]')
      .evaluateAll((links) => links.map((link) => link.getAttribute('href') ?? ''));
  const tabs = page.getByRole('navigation', { name: 'Accesos rápidos' });
  const found = [...(await hrefs(tabs))];
  found.push(...(await hrefs(await fullMenu(page))));
  found.push(...(await hrefs(page.getByRole('banner'))));
  found.push('/account');
  return [...new Set(found.filter((href) => href.startsWith('/') && !href.includes('#')))];
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

/**
 * El menú completo (VIG-364): en escritorio es el lateral; en tableta no hay lateral y «Menú» de la
 * barra inferior («Accesos rápidos») lo despliega.
 */
async function fullMenu(page: Page): Promise<Locator> {
  await shellReady(page);
  const nav = page.getByRole('navigation', { name: 'Navegación principal' });
  const toggle = page
    .getByRole('navigation', { name: 'Accesos rápidos' })
    .getByRole('button', { name: 'Menú', exact: true });
  if (await nav.isVisible()) return nav;
  // Sin «Menú» (todo el menú cabe en la barra inferior), el menú plegado no ofrece nada más.
  if ((await toggle.count()) === 0) return nav;
  await toggle.click();
  await expect(nav).toBeVisible();
  return nav;
}

/**
 * Espera al armazón (cabecera y, en tableta, la barra inferior o, en escritorio, el menú) antes de
 * contar entradas: `count()` no espera y, recién navegada la página, daría cero.
 */
async function shellReady(page: Page): Promise<void> {
  await expect(page.getByRole('banner')).toBeVisible();
  await expect(
    page
      .getByRole('navigation', { name: 'Accesos rápidos' })
      .or(page.getByRole('navigation', { name: 'Navegación principal' }))
      .first(),
  ).toBeVisible();
}

/** La navegación principal de la aplicación: el recorrido diario no escribe direcciones. */
export async function openMenu(page: Page, name: string): Promise<void> {
  await shellReady(page);
  const tab = page
    .getByRole('navigation', { name: 'Accesos rápidos' })
    .getByRole('link', { name, exact: true });
  if ((await tab.count()) > 0) {
    await tab.click();
    return;
  }
  await (await fullMenu(page)).getByRole('link', { name, exact: true }).click();
}

/**
 * Entrada del menú que necesita zona o planta (VIG-363): abre su selector del alcance, elige
 * `value` y pulsa «Abrir». Solo clics: la dirección no se escribe.
 */
export async function openMenuWith(page: Page, name: string, value: string): Promise<void> {
  const nav = await fullMenu(page);
  await nav.getByRole('button', { name, exact: true }).click();
  const form = nav.getByRole('form', { name, exact: true });
  await form.getByRole('combobox').selectOption(value);
  await form.getByRole('button', { name: 'Abrir', exact: true }).click();
}

/**
 * Inicio propio del rol (VIG-363, hallazgo 18): la marca de la cabecera lleva al inicio del rol,
 * con su título, y el panel del coordinador (`/home`) lleva a cualquier otro rol a ese mismo
 * inicio. Nadie salvo el coordinador ve «Inicio del coordinador» ni su entrada en el menú.
 */
export async function expectOwnHome(page: Page, title: string, path: string): Promise<void> {
  // Desde una pantalla que toda sesión abre (su cuenta), con la cabecera del armazón.
  await page.goto('/account');
  await page.getByRole('link', { name: 'Vigía', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(title);
  // Todos los inicios se titulan «Inicio» (VIG-365): el del coordinador es el que vive en `/home`.
  const coordinator = path === '/home';
  // El inicio de cada rol se llama «Inicio» en el menú (VIG-364): solo el del coordinador lleva al
  // panel `/home`. En tableta el menú lateral está plegado y el inicio va en la barra inferior.
  await expect(
    page.locator(
      [
        'nav[aria-label="Navegación principal"] a[href="/home"]:visible',
        'nav[aria-label="Accesos rápidos"] a[href="/home"]:visible',
      ].join(', '),
    ),
  ).toHaveCount(coordinator ? 1 : 0);
  if (coordinator) return;
  await page.goto('/home');
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(title);
}

/**
 * Espera a que `target` exista y ninguna sección de la pantalla cargue ni refresque (el botón de
 * actualizar está deshabilitado mientras lee). Mientras llegan las lecturas, el panel y la bandeja
 * cambian de alto y un enlace se desplaza miles de píxeles (medido en Firefox): un clic en ese
 * intervalo no navegaba (VIG-361).
 */
async function settled(page: Page, target: Locator): Promise<void> {
  const loading = page
    .getByRole('main')
    .locator('[aria-busy="true"], [data-policy] button:disabled');
  await expect
    .poll(async () => (await target.count()) > 0 && (await loading.count()) === 0, {
      message: 'La pantalla terminó sus lecturas',
    })
    .toBe(true);
}

/** La bandeja completa, desde su entrada del menú (VIG-363: cada entrada con el nombre de su página). */
export async function openInbox(page: Page): Promise<void> {
  await openMenu(page, 'Hallazgos');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Bandeja de hallazgos');
  await settled(page, page.locator('tbody tr'));
}

/**
 * Pulsa el acceso `href` de una fila de la bandeja ya abierta. El tamaño del mundo depende de la
 * semilla de carga (37 y 46 filas medidas) y los hallazgos reservados quedan al final, del más
 * nuevo al más viejo: si no están en la primera página de 50, se piden más páginas desde la
 * interfaz, como lo haría la persona (VIG-361).
 */
export async function openFromInbox(page: Page, href: string): Promise<void> {
  const link = page.locator(`a[href="${href}"]`);
  const more = page.getByRole('button', { name: 'Cargar más hallazgos', exact: true });
  const rows = page.locator('tbody tr');
  await settled(page, rows);
  while ((await link.count()) === 0 && (await more.count()) > 0) {
    const before = await rows.count();
    await more.click();
    await expect.poll(() => rows.count()).toBeGreaterThan(before);
    await settled(page, rows);
  }
  await link.click();
}

/** El panel «Siguiente paso» del detalle y del lazo (VIG-365): una sola acción principal. */
export function nextSteps(page: Page): Locator {
  return page.getByRole('region', { name: 'Siguiente paso', exact: true });
}

/**
 * En escritorio, el panel va en la columna derecha a la altura del inicio de la columna principal,
 * no al pie de ella (sistema visual v1).
 */
export async function expectNextStepBeside(page: Page): Promise<void> {
  const offset = await nextSteps(page).evaluate((panel) => {
    const aside = panel.closest('aside');
    const main = aside?.previousElementSibling;
    if (!aside || !main) return Number.NaN;
    const side = aside.getBoundingClientRect();
    const column = main.getBoundingClientRect();
    return side.left >= column.right ? Math.abs(side.top - column.top) : Number.NaN;
  });
  expect(offset).toBeLessThanOrEqual(1);
}

/**
 * Una sola actualización por página (VIG-365, S3-03): con la pantalla ya cargada, cuenta los
 * botones «Actualizar» de la zona principal.
 */
export async function expectOneRefresh(page: Page): Promise<void> {
  await expect(
    page.getByRole('main').getByRole('button', { name: 'Actualizar', exact: true }),
  ).toHaveCount(1);
}

/** Sin desborde horizontal de la página (S3-10): lo ancho va en su caja con desplazamiento. */
export async function expectNoPageOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/**
 * Confirma el hallazgo desde la pantalla de clasificación ya abierta (se llega con clics, VIG-359).
 * Las escrituras se hacen siempre desde la interfaz; solo se observa su respuesta real.
 */
export async function confirmFinding(page: Page, findingId: string, reason: string) {
  await expect(page).toHaveURL(new RegExp(`/findings/${findingId}/classify$`));
  // La pantalla abre cargada (VIG-365). El historial llega y se pinta encima del formulario: se
  // espera a que termine para que el clic no caiga en el hueco que deja al bajar (VIG-364).
  const history = page.getByRole('region', { name: 'Historial de clasificaciones', exact: true });
  await expect(history).toBeVisible();
  const form = page.getByRole('form', { name: 'Registrar una clasificación' });
  await settled(page, form);
  await form.getByRole('radio', { name: 'Confirmado', exact: true }).check();
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
  await expect(history.getByText(reason, { exact: true })).toBeVisible();
  await expect(page.locator('[data-role-in-use="coordinator_sst"]')).toContainText(
    'Coordinador SST',
  );
  await expect(history).toContainText('con el rol Coordinador SST');
  // VIG-365 (S3-20): registrada, el formulario se pliega sin errores y el recibo da el lazo.
  await expect(form).toHaveCount(0);
  await expect(page.getByRole('main').getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Ir al lazo', exact: true })).toBeVisible();
}

export async function logout(page: Page) {
  // Marcadores sintéticos: la comprobación no pasa de forma vacía si se elimina la limpieza.
  await page.evaluate(() => {
    localStorage.setItem('e2e-cleanup', 'sintetico');
    sessionStorage.setItem('e2e-cleanup', 'sintetico');
  });
  await page.getByRole('button', { name: 'Cuenta', exact: true }).click();
  const sent = page.waitForRequest(
    (request) => request.method() === 'POST' && new URL(request.url()).pathname === '/auth/logout',
  );
  await page.getByRole('button', { name: 'Cerrar sesión', exact: true }).click();
  // La aplicación vuelve a /login cuando `POST /auth/logout` termina, con respuesta o sin ella
  // (tope de escritura de 30 s). Se espera ese evento: un tope fijo menor dejaba la página en /home
  // con la limpieza aún pendiente si el mundo compartido tardaba en responder (VIG-361).
  await (await sent).response();
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
