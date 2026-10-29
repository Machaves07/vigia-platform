import { expect, type BrowserContext, type Page } from '@playwright/test';

/**
 * Agregado del arnés en el worker (VIG-336): contextos y páginas vigilados, documentos del propio
 * origen servidos con la política de producción, violaciones de CSP o tipos confiables, políticas
 * de tipos confiables que la aplicación intentó registrar y salidas de datos del cliente.
 */
interface PolicyTotals {
  contexts: number;
  pages: number;
  documents: number;
  violations: number;
  trustedTypesPolicies: number;
  clientDataIssues: number;
}
const totals: PolicyTotals = {
  contexts: 0,
  pages: 0,
  documents: 0,
  violations: 0,
  trustedTypesPolicies: 0,
  clientDataIssues: 0,
};

export function policyTotals(): Readonly<PolicyTotals> {
  return { ...totals };
}

/** Línea del resumen del arnés (Evidence de VIG-336). */
export function policySummary(): string {
  return (
    `[política] contextos ${String(totals.contexts)}, páginas ${String(totals.pages)}, ` +
    `documentos con la política de producción ${String(totals.documents)}, ` +
    `violaciones de CSP o tipos confiables ${String(totals.violations)}, ` +
    `políticas de tipos confiables registradas ${String(totals.trustedTypesPolicies)}, ` +
    `datos en el cliente ${String(totals.clientDataIssues)}`
  );
}

/** Directivas que todo documento del propio origen debe traer (PAT-APP-SEG-01 y 04). */
const PRODUCTION_DIRECTIVES = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "frame-src 'none'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "require-trusted-types-for 'script'",
] as const;

/**
 * Rutas declaradas por las pantallas (`SCREENS`; el arnés de Playwright no carga `src/`, así que
 * la lista se copia aquí y `tests/unit/e2e/policy.test.ts` comprueba que coincide).
 */
export const DECLARED_ROUTES: readonly string[] = [
  '/account',
  '/admin/hierarchy',
  '/admin/organization',
  '/admin/users',
  '/admin/vocabulary',
  '/concessions',
  '/exports',
  '/findings',
  '/findings/:finding_state_id',
  '/findings/:finding_state_id/classify',
  '/findings/:finding_state_id/loop',
  '/findings/:finding_state_id/loop/closure',
  '/findings/:finding_state_id/loop/reopening',
  '/findings/:finding_state_id/loop/unclosable',
  '/findings/:finding_state_id/root-cause',
  '/findings/labels',
  '/findings/loops',
  '/fleet',
  '/fleet/alarms',
  '/fleet/enrollment',
  '/fleet/nodes/:node_id',
  '/fleet/nodes/:node_id/enrollment',
  '/fleet/nodes/:node_id/lifecycle',
  '/fleet/target-versions',
  '/home',
  '/integrity',
  '/invitations/:token',
  '/login',
  '/login/enroll',
  '/login/second-factor',
  '/metrics',
  '/metrics/weekly',
  '/notifications',
  '/notifications/preferences',
  '/plants/:plant_id/admissions',
  '/plants/:plant_id/catalog',
  '/plants/:plant_id/fleet-thresholds',
  '/plants/:plant_id/gates',
  '/plants/:plant_id/policy',
  '/provider/concessions',
  '/review-queue',
  '/transparency',
  '/transparency/:zone_id',
  '/zones/:zone_id/catalog',
  '/zones/:zone_id/catalog/parameters',
  '/zones/:zone_id/commissioning',
  '/zones/:zone_id/commissioning/close',
  '/zones/:zone_id/commissioning/matrix',
  '/zones/:zone_id/commissioning/occlusion',
  '/zones/:zone_id/commissioning/records/:record_id',
  '/zones/:zone_id/coverage',
  '/zones/:zone_id/gates',
  '/zones/:zone_id/monthly/:period',
  '/zones/:zone_id/parameters',
  '/zones/:zone_id/standards',
  '/zones/monthly',
];

/** Lecturas `GET` de la API que una pantalla abre como documento (el acta de comisionamiento). */
export const API_DOCUMENTS: readonly string[] = ['/commissioning-records/:record_id/document'];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** Forma de cada parámetro de ruta; cualquier otro nombre sería texto libre posible. */
const PARAM_FORMS: Readonly<Record<string, RegExp>> = {
  zone_id: UUID,
  plant_id: UUID,
  finding_state_id: UUID,
  node_id: UUID,
  record_id: UUID,
  period: /^\d{4}-(?:0[1-9]|1[0-2])$/,
  // Token opaco del enlace de invitación (BR-APP-18): base64url, sin datos de la persona.
  token: /^[A-Za-z0-9_-]{16,512}$/,
};

function matches(segments: readonly string[], template: string): boolean {
  const expected = template.split('/').slice(1);
  return (
    expected.length === segments.length &&
    expected.every((part, index) => {
      const segment = segments[index] ?? '';
      if (!part.startsWith(':')) return part === segment;
      return PARAM_FORMS[part.slice(1)]?.test(decodeURIComponent(segment)) === true;
    })
  );
}

/**
 * Problema de una URL del propio origen (NFR-APP-32): una ruta declarada (o un documento de la
 * API) con cada parámetro de su forma cerrada; sin consulta y como mucho el ancla de un elemento.
 */
export function urlProblem(url: URL): string | null {
  if (url.search !== '') return 'consulta en la URL';
  if (!/^(?:#[A-Za-z0-9_-]{1,80})?$/.test(url.hash)) return 'fragmento con datos en la URL';
  if (url.pathname === '/') return null;
  const segments = url.pathname.split('/').slice(1);
  const known = [...DECLARED_ROUTES, ...API_DOCUMENTS].some((template) =>
    matches(segments, template),
  );
  return known ? null : 'texto libre en la ruta';
}

/**
 * `media-src` y `connect-src` de `policy` admiten exactamente el propio origen y el del depósito
 * (`VIGIA_CSP_STORE_ORIGINS` del mundo); devuelve la directiva que no lo hace, o `null`.
 */
function storeDirectivesProblem(policy: readonly string[], storeOrigin: string): string | null {
  for (const name of ['media-src', 'connect-src']) {
    const directive = policy.find((entry) => entry.split(/\s+/)[0] === name);
    const sources = new Set(directive?.split(/\s+/).slice(1));
    const expected = new Set(["'self'", storeOrigin]);
    if (sources.size !== expected.size || [...expected].some((source) => !sources.has(source))) {
      return name;
    }
  }
  return null;
}

/**
 * Cubre páginas, marcos, ventanas nuevas y navegación, antes del primer guion de la app. Con
 * `origin`, además exige la política de producción en cada documento de ese origen (con
 * `storeOrigin`, también `media-src` y `connect-src` con el depósito), rechaza las URL con texto
 * libre y cualquier escritura de los guiones de la aplicación en la consola.
 */
export async function watchPolicy(
  context: BrowserContext,
  label = '',
  origin?: string,
  storeOrigin?: string,
): Promise<() => void> {
  const violations: string[] = [];
  const clientData: string[] = [];
  totals.contexts += 1;
  await context.exposeBinding('__vigiaPolicyViolation', (_source, directive: string) => {
    violations.push(directive);
    if (directive === 'trusted-types: createPolicy') totals.trustedTypesPolicies += 1;
  });
  await context.addInitScript(() => {
    const report = (directive: string): void => {
      void (
        globalThis as unknown as {
          __vigiaPolicyViolation: (directive: string) => Promise<void>;
        }
      ).__vigiaPolicyViolation(directive);
    };
    document.addEventListener('securitypolicyviolation', (event) => {
      report(event.effectiveDirective);
    });
    // PAT-APP-SEG-04: la aplicación no registra ninguna política de tipos confiables (Chromium).
    const factory = (
      globalThis as unknown as {
        trustedTypes?: { createPolicy: (...args: unknown[]) => unknown };
      }
    ).trustedTypes;
    if (factory !== undefined) {
      const createPolicy = factory.createPolicy.bind(factory);
      factory.createPolicy = (...args: unknown[]) => {
        report('trusted-types: createPolicy');
        return createPolicy(...args);
      };
    }
  });
  const watchPage = (page: Page) => {
    totals.pages += 1;
    page.on('console', (message) => {
      const text = message.text();
      if (
        /(?:content.security.policy|trusted.?types?|trustedhtml|trustedscript)/i.test(text) &&
        /(?:refused|violat|blocked|requires|requirement|sink)/i.test(text)
      ) {
        // No copiar el mensaje: podría contener entradas o URL con credenciales.
        violations.push('console: CSP/Trusted Types');
      } else if (origin !== undefined && message.location().url.startsWith(`${origin}/assets/`)) {
        // La consola de producción está vacía (NFR-APP-32): ni el tipo ni el texto se copian.
        clientData.push('consola: escritura de la aplicación');
      }
    });
    page.on('pageerror', (error) => {
      if (/trustedhtml|trustedscript|trusted.?types/i.test(error.message)) {
        violations.push('pageerror: Trusted Types');
      }
    });
    if (origin === undefined) return;
    page.on('response', (response) => {
      const request = response.request();
      if (request.resourceType() !== 'document' || !response.url().startsWith(`${origin}/`)) {
        return;
      }
      const policy = (response.headers()['content-security-policy'] ?? '')
        .split(';')
        .map((directive) => directive.trim());
      if (
        PRODUCTION_DIRECTIVES.every((directive) => policy.includes(directive)) &&
        (storeOrigin === undefined || storeDirectivesProblem(policy, storeOrigin) === null)
      ) {
        totals.documents += 1;
      } else {
        violations.push('documento sin la política de producción');
      }
    });
    page.on('framenavigated', (frame) => {
      if (frame !== page.mainFrame()) return;
      // Un marco puede navegar con la URL vacía (o no absoluta) mientras se crea: no es un
      // documento del origen, cuya política ya exige el `response` de arriba (VIG-356).
      if (!URL.canParse(frame.url())) return;
      const url = new URL(frame.url());
      if (url.origin !== origin) return;
      const problem = urlProblem(url);
      if (problem !== null) clientData.push(problem);
    });
  };
  context.on('page', watchPage);
  for (const page of context.pages()) watchPage(page);
  // La comprobación se repite (por prueba y al cerrar el worker): el agregado cuenta cada una vez.
  const counted = { violations: 0, clientData: 0 };
  return () => {
    totals.violations += violations.length - counted.violations;
    totals.clientDataIssues += clientData.length - counted.clientData;
    counted.violations = violations.length;
    counted.clientData = clientData.length;
    expect(violations, `Violaciones de CSP o Trusted Types${label ? ` (${label})` : ''}`).toEqual(
      [],
    );
    expect(clientData, `Datos en el cliente${label ? ` (${label})` : ''}`).toEqual([]);
  };
}

/** Clave única del almacenamiento: `UiPreferences` (`src/state/scope/preferences.ts`). */
const PREFERENCES_KEY = 'vigia.ui_preferences';

/**
 * Al terminar un recorrido (NFR-APP-32, PAT-APP-SEG-07): el título de la pestaña es el fijo, el
 * almacenamiento solo tiene `UiPreferences` y no hay bases IndexedDB. Sin copiar ningún valor.
 */
export async function assertClientState(page: Page, origin: string): Promise<void> {
  if (page.isClosed() || !page.url().startsWith(`${origin}/`)) return;
  const state = await page.evaluate(async () => {
    const databases =
      typeof indexedDB.databases === 'function' ? (await indexedDB.databases()).length : 0;
    return {
      title: document.title,
      local: Object.keys(localStorage),
      session: sessionStorage.length,
      databases,
    };
  });
  const issues = [
    ...(state.title === 'Vigía' ? [] : ['título de la pestaña con datos']),
    ...state.local.filter((key) => key !== PREFERENCES_KEY).map(() => 'clave de localStorage'),
    ...(state.session === 0 ? [] : ['sessionStorage con datos']),
    ...(state.databases === 0 ? [] : ['base IndexedDB']),
  ];
  totals.clientDataIssues += issues.length;
  expect(issues, 'Datos en el cliente al terminar el recorrido').toEqual([]);
}
