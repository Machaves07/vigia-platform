/**
 * NFR-APP-50 en navegadores reales (VIG-336): `@axe-core/playwright` sobre las pantallas de cada
 * rol contra el mundo local (API real, construcción de producción y política de contenido
 * activa), sin violaciones de impacto serio ni crítico con las reglas WCAG 2.1 A y AA. Aquí sí
 * cuentan el contraste y el tamaño de los objetivos táctiles, que jsdom no calcula
 * (`tests/quality/a11y_all_screens.test.tsx`).
 *
 * Los roles de escritorio corren en los proyectos de escritorio y los de campo (instalador y
 * COPASST) en los de tableta. Cada pantalla se analiza con su contenido cargado: título de la
 * pantalla visible, ningún indicador ni texto de carga y ningún estado de error de sección. Las
 * pantallas de consulta auditada (SCR-09 y SCR-11) se analizan después de pulsar la consulta.
 */
import AxeBuilder from '@axe-core/playwright';
import type { Page } from '@playwright/test';

import { authenticatedTest, expect, test } from './fixtures';
import { installerRoute, required } from './journeys';
import type { Role, World } from './world';

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];
const BLOCKING = new Set(['serious', 'critical']);

/** Con uno de estos textos en `<main>` la pantalla no muestra su contenido. */
const NOT_CONTENT = [
  'No se pudo cargar esta pantalla.',
  'Cargando…',
  'Cargando la aplicación…',
  'Cargando datos…',
];

/** Analiza la página y devuelve la línea del informe; falla con las serias o críticas. */
async function audit(page: Page, label: string): Promise<void> {
  await expect(page.locator('main h1').first()).toBeVisible();
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
  for (const value of NOT_CONTENT) {
    await expect(page.locator('main').getByText(value, { exact: true }), label).toHaveCount(0, {
      timeout: 30_000,
    });
  }
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const count = (impact: string) =>
    results.violations.filter((violation) => violation.impact === impact).length;
  process.stdout.write(
    `[axe-e2e] ${label}: críticas ${String(count('critical'))}, serias ${String(count('serious'))}, ` +
      `moderadas ${String(count('moderate'))}, menores ${String(count('minor'))}\n`,
  );
  const blocking = results.violations
    .filter((violation) => BLOCKING.has(violation.impact ?? ''))
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target.join(' ')),
    }));
  expect(blocking, label).toEqual([]);
}

/** Rutas de cada rol con los identificadores sintéticos del mundo. */
function routes(world: World, role: Role): readonly string[] {
  const zone = required(world.zone_ids[0]);
  const plant = required(world.plant_ids[0]);
  switch (role) {
    case 'coordinator_sst':
      return [
        '/home',
        '/findings',
        '/findings/labels',
        '/review-queue',
        '/notifications',
        '/notifications/preferences',
        `/zones/${zone}/coverage`,
        `/zones/${zone}/catalog`,
        `/plants/${plant}/gates`,
        '/account',
      ];
    case 'plant_manager':
      return [
        '/metrics',
        '/metrics/weekly',
        '/zones/monthly',
        `/zones/${zone}/monthly/${world.journeys.period}`,
        '/exports',
        '/concessions',
        '/integrity',
      ];
    case 'line_manager':
      return ['/home', `/findings/${world.journeys.line_finding_id}`];
    case 'administrator':
      return [
        '/admin/users',
        '/admin/hierarchy',
        '/admin/organization',
        '/admin/vocabulary',
        `/plants/${plant}/admissions`,
      ];
    case 'copasst':
      return ['/transparency', `/transparency/${zone}`];
    case 'provider_installer':
      return ['/provider/concessions'];
  }
}

/**
 * Pantallas de consulta auditada del coordinador (SCR-09 y SCR-11) con los hallazgos del motor:
 * se pulsa la consulta (si la pantalla no abre cargada, VIG-365) y se espera su contenido antes de
 * analizar. Solo lecturas, salvo el inicio de la causa raíz si no hay ninguno en curso (VIG-363: si
 * lo hay, se retoma); el recorrido del coordinador retoma o inicia igual.
 */
function auditedRoutes(
  world: World,
  engine: string,
): readonly {
  readonly path: string;
  /** La consulta que se pulsa; `null` si la pantalla abre cargada. */
  readonly button: string | RegExp | null;
  readonly content: string;
}[] {
  const selected = required(world.journeys.findings[engine]);
  const history = 'Historial de evidencia, cierres y declaraciones';
  return [
    {
      path: `/findings/${selected.closure}/classify`,
      // Clasificar y el lazo abren cargados (VIG-365): sin botón de consulta.
      button: null,
      content: 'Historial de clasificaciones',
    },
    {
      path: `/findings/${selected.closure}/loop`,
      button: null,
      // Lo que se ofrece depende del estado (VIG-365); el panel «Siguiente paso» está en todos.
      content: 'Siguiente paso',
    },
    {
      path: `/findings/${selected.closure}/loop/closure`,
      button: 'Consultar el expediente de cierre',
      content: history,
    },
    {
      path: `/findings/${selected.unclosable}/loop/unclosable`,
      button: 'Consultar el expediente de cierre',
      content: history,
    },
    {
      path: `/findings/${selected.unclosable}/loop/reopening`,
      button: 'Consultar el expediente de cierre',
      content: history,
    },
    {
      path: `/findings/${selected.unclosable}/root-cause`,
      // Con un análisis en curso se retoma; sin él, se inicia (VIG-363).
      button: /^(Iniciar el análisis de causa raíz|Continuar el análisis)$/,
      content: 'Plantilla guiada de causa raíz',
    },
  ];
}

/** Rutas del instalador bajo concesión (se activa desde el panel, como en su recorrido). */
function installerRoutes(world: World): readonly string[] {
  const zone = required(world.zone_ids[0]);
  return [
    '/fleet',
    '/fleet/alarms',
    '/fleet/enrollment',
    `/zones/${zone}/commissioning`,
    `/zones/${zone}/commissioning/matrix`,
  ];
}

test('acceso público: inicio de sesión sin violaciones serias', async ({ page }) => {
  await page.goto('/login');
  await audit(page, 'público /login');
});

const DESKTOP: readonly Role[] = [
  'coordinator_sst',
  'plant_manager',
  'line_manager',
  'administrator',
];
const TABLET: readonly Role[] = ['copasst', 'provider_installer'];

for (const role of [...DESKTOP, ...TABLET]) {
  authenticatedTest.describe(`rol ${role}`, () => {
    authenticatedTest.use({ role });
    authenticatedTest.skip(
      ({ hasTouch }) => hasTouch !== TABLET.includes(role),
      TABLET.includes(role) ? 'Rol de campo: tableta.' : 'Rol de escritorio.',
    );

    authenticatedTest(
      `pantallas de ${role} sin violaciones serias ni críticas`,
      async ({ authenticatedPage: page, world, browserName }) => {
        authenticatedTest.setTimeout(300_000);
        for (const path of routes(world, role)) {
          await page.goto(path);
          await audit(page, `${role} ${path.replace(/[0-9a-f-]{36}/g, ':id')}`);
        }
        if (role === 'coordinator_sst') {
          for (const { path, button, content } of auditedRoutes(world, browserName)) {
            await page.goto(path);
            if (button !== null) {
              await page.getByRole('button', { name: button, exact: true }).click();
            }
            await expect(
              page.locator('main').getByText(content, { exact: true }).first(),
            ).toBeVisible({
              timeout: 60_000,
            });
            await audit(
              page,
              `${role} ${path.replace(/[0-9a-f-]{36}/g, ':id')} (tras «${String(button ?? 'abrir')}»)`,
            );
          }
        }
        if (role === 'provider_installer') {
          for (const path of installerRoutes(world)) {
            await installerRoute(page, world, path);
            await audit(page, `${role} ${path.replace(/[0-9a-f-]{36}/g, ':id')}`);
          }
        }
      },
    );
  });
}
