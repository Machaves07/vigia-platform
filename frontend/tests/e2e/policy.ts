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
  "frame-ancestors 'none'",
  "require-trusted-types-for 'script'",
] as const;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const WORD = /^[a-z][a-z0-9-]*$/;
const PERIOD = /^\d{4}-\d{2}$/;
const INVITATION_TOKEN = /^[A-Za-z0-9_-]{16,512}$/;

/**
 * Problema de una URL del propio origen (NFR-APP-32): segmentos fijos, identificadores UUID, el
 * mes o el token opaco de invitación; sin consulta y como mucho el ancla de un elemento.
 */
export function urlProblem(url: URL): string | null {
  if (url.search !== '') return 'consulta en la URL';
  if (!/^(?:#[A-Za-z0-9_-]{1,80})?$/.test(url.hash)) return 'fragmento con datos en la URL';
  const segments = url.pathname
    .split('/')
    .slice(1)
    .filter((segment) => segment !== '');
  for (const [index, segment] of segments.entries()) {
    if (WORD.test(segment) || UUID.test(segment) || PERIOD.test(segment)) continue;
    if (segments[index - 1] === 'invitations' && INVITATION_TOKEN.test(segment)) continue;
    return 'texto libre en la ruta';
  }
  return null;
}

/**
 * Cubre páginas, marcos, ventanas nuevas y navegación, antes del primer guion de la app. Con
 * `origin`, además exige la política de producción en cada documento de ese origen, rechaza las
 * URL con texto libre y cualquier escritura de los guiones de la aplicación en la consola.
 */
export async function watchPolicy(
  context: BrowserContext,
  label = '',
  origin?: string,
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
      if (PRODUCTION_DIRECTIVES.every((directive) => policy.includes(directive))) {
        totals.documents += 1;
      } else {
        violations.push('documento sin la política de producción');
      }
    });
    page.on('framenavigated', (frame) => {
      if (frame !== page.mainFrame()) return;
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
