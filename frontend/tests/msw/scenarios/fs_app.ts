/**
 * Escenarios de inyección de fallos de la interfaz FS-APP-01 a 10 (PAT-APP-RES-08, LC-APP-26)
 * como variantes de los manejadores generados. Cada prueba de `tests/resilience/` los aplica con
 * `server.use(...scenario('fs_app_…'))` y gobierna el fallo con `faults`, que `resetFaults()`
 * deja como al empezar. Todos los datos son sintéticos; los tiempos los marca el `FakeClock` de la
 * prueba, nunca la hora del sistema.
 */
import { http, HttpResponse, type HttpHandler, type HttpResponseResolver } from 'msw';

import type { HierarchyView } from '@/ports/shared';

import { apiOperation, API_OPERATIONS } from '../../generators/openapi';
import { apiError, bySpecificity, mswPath, operationResponse } from '../handlers';

export const FS_ORGANIZATION = '0190a1b2-0000-7000-8000-0000000000f1';
export const FS_PLANT = '0190a1b2-0000-7000-8000-0000000004a1';
export const FS_ZONES = [1, 2, 3].map((index) => ({
  zone_id: `0190a1b2-1111-7000-8000-${String(index).padStart(12, '0')}`,
  plant_id: FS_PLANT,
  code: `Z-${String(index)}`,
  name: `Zona sintética ${String(index)}`,
  node_id: null,
}));

/** `GET /hierarchy` con una planta de tres zonas (SCR-14 necesita zonas que elegir). */
export function fsHierarchy(): HierarchyView {
  return {
    organization: {
      organization_id: FS_ORGANIZATION,
      code: 'ORG-SINT',
      name: 'Organización sintética',
      kind: 'client',
    },
    plants: [
      {
        plant_id: FS_PLANT,
        code: 'PL-SINT',
        name: 'Planta sintética',
        timezone: 'America/Bogota',
        country: 'CO',
        status: 'active',
        data_region: 'co',
        zones: FS_ZONES,
      },
    ],
    nodes: [],
  } as unknown as HierarchyView;
}

/** Una petición que no responde hasta que se libera; liberada, termina como fallo de red. */
interface Hung {
  readonly release: () => void;
}

/** Interruptores de los escenarios. Solo las pruebas los cambian; `resetFaults` los restaura. */
export const faults = {
  /** FS-APP-01: la plataforma (API y estáticos del mismo origen) no responde. */
  platformDown: false,
  /** FS-APP-02: cuántas escrituras más fallan; FS-APP-02 y 03: `retry_after_seconds`. */
  writeFailures: 0,
  /** FS-APP-03: el límite por origen rechaza toda petición con sesión. */
  rateLimited: false,
  retryAfterSeconds: 0,
  /** FS-APP-04: el almacén de evidencias no está disponible. */
  storageDown: false,
  /** FS-APP-05: vigencia en segundos de cada URL de evidencia emitida. */
  evidenceUrlSeconds: 300,
  /** FS-APP-06: la sesión venció en el servidor; la vuelve a abrir `POST /auth/login`. */
  sessionExpired: false,
  /** FS-APP-06: el inicio de sesión pide el segundo factor antes de completar la sesión. */
  secondFactor: false,
  /** FS-APP-07: hay una versión nueva del aviso de tratamiento sin aceptar. */
  noticePending: false,
  noticeVersion: 'v0-pendiente',
  /** FS-APP-08: versión que anuncia la API (`null`: la de la construcción). */
  apiVersion: null as string | null,
  /** Peticiones retenidas por `platformDown`. */
  hung: [] as Hung[],
  /** URL de evidencia emitidas, en orden (FS-APP-05). */
  issuedUrls: [] as string[],
};

/** Libera las peticiones retenidas y deja los interruptores como al empezar. */
export function resetFaults(): void {
  for (const request of faults.hung.splice(0)) request.release();
  faults.platformDown = false;
  faults.writeFailures = 0;
  faults.rateLimited = false;
  faults.retryAfterSeconds = 0;
  faults.storageDown = false;
  faults.evidenceUrlSeconds = 300;
  faults.sessionExpired = false;
  faults.secondFactor = false;
  faults.noticePending = false;
  faults.noticeVersion = 'v0-pendiente';
  faults.apiVersion = null;
  faults.issuedUrls.length = 0;
}

/** FS-APP-01: la plataforma vuelve a responder; lo retenido termina como fallo de red. */
export function restorePlatform(): void {
  faults.platformDown = false;
  for (const request of faults.hung.splice(0)) request.release();
}

/** El instante de la prueba lo da su reloj; el manejador solo lo recibe. */
let now: () => number = () => 0;
export function setScenarioClock(clock: () => number): void {
  now = clock;
}

/**
 * Origen de la API en los escenarios: el de `mountShell` y los arneses de pantalla. Los
 * manejadores generados usan rutas relativas al origen de la página de jsdom (con puerto), así
 * que aquí se declaran con el origen explícito.
 */
export const FS_ORIGIN = 'http://localhost';

/** Variante de `operationId` en `FS_ORIGIN`. */
function at(operationId: string, resolver: HttpResponseResolver): HttpHandler {
  const operation = apiOperation(operationId);
  return http[operation.method](`${FS_ORIGIN}${mswPath(operation.path)}`, resolver);
}

export const FS_EXPORT = '0190a1b2-1414-7000-8000-0000000004a1';

/** Trabajo de exportación recién aceptado (`ExportOut` sintético). */
export function fsExportJob(body: {
  zone_ids?: unknown;
  period?: unknown;
  package_form?: unknown;
}) {
  const instant = new Date(now()).toISOString();
  return {
    export_id: FS_EXPORT,
    plant_id: FS_PLANT,
    zone_ids: Array.isArray(body.zone_ids) ? body.zone_ids : [],
    period: body.period ?? { from: instant, to: instant },
    package_form: body.package_form === 'with_clips' ? 'with_clips' : 'without_clips',
    export_state: 'requested',
    progress_percent: 0,
    current_artifact: null,
    attempt_count: 0,
    failure_code: null,
    requested_at: instant,
    started_at: null,
    completed_at: null,
    expires_at: null,
    correlation_id: FS_EXPORT,
    manifest_summary: null,
  };
}

/** Solicitud de exportación aceptada (202) y su estado; la usan los escenarios de SCR-14. */
const exportAccepted: HttpHandler[] = [
  at('request_export_accreditation_exports_post', async ({ request }) =>
    HttpResponse.json(fsExportJob((await request.json()) as Record<string, unknown>), {
      status: 202,
    }),
  ),
  at('export_status_accreditation_exports__export_id__get', () =>
    HttpResponse.json(fsExportJob({})),
  ),
];

/**
 * Base de las pantallas con el armazón: jerarquía con zonas, campana vacía y, para el resto de
 * operaciones de `app.yaml`, el ejemplo generado servido también en `FS_ORIGIN`.
 */
const base: HttpHandler[] = [
  at('hierarchy_hierarchy_get', () => HttpResponse.json(fsHierarchy())),
  at('list_notifications_notifications_get', () =>
    HttpResponse.json({ items: [], next_after: null }),
  ),
  ...exportAccepted,
  ...[...API_OPERATIONS]
    .sort(bySpecificity)
    .map((operation) => at(operation.operationId, () => operationResponse(operation.operationId))),
];

/**
 * FS-APP-01: con `platformDown`, toda petición al origen queda sin respuesta (el transporte la
 * corta a los 10 s o 30 s); sin él, pasa a los manejadores siguientes.
 */
const platformTimeout: HttpHandler[] = [
  http.all('*', async ({ request }) => {
    if (!faults.platformDown || new URL(request.url).origin !== FS_ORIGIN) return undefined;
    await new Promise<void>((resolve) => {
      faults.hung.push({ release: resolve });
    });
    return HttpResponse.error();
  }),
];

/** FS-APP-02: `POST /accreditation/exports` responde `503 temporarily_unavailable` `writeFailures` veces. */
const writeUnavailable: HttpHandler[] = [
  at('request_export_accreditation_exports_post', () => {
    if (faults.writeFailures <= 0) return undefined;
    faults.writeFailures -= 1;
    return apiError('temporarily_unavailable', 503, {
      retryAfterSeconds: faults.retryAfterSeconds,
    });
  }),
];

/**
 * FS-APP-03: con `rateLimited`, el límite por origen de U-02 rechaza con `429 rate_limited` toda
 * petición con sesión, lecturas y escrituras; la sonda pública no pasa por él.
 */
const rateLimited: HttpHandler[] = [
  http.all('*', ({ request }) => {
    const url = new URL(request.url);
    if (!faults.rateLimited || url.origin !== FS_ORIGIN || url.pathname === '/health/live') {
      return undefined;
    }
    return apiError('rate_limited', 429, { retryAfterSeconds: faults.retryAfterSeconds });
  }),
];

/** FS-APP-04: la URL de lectura y la concesión de subida responden `storage_unavailable`. */
const storageUnavailable: HttpHandler[] = [
  at('read_url_evidence__evidence_id__read_url_post', ({ params }) =>
    faults.storageDown
      ? apiError('storage_unavailable', 503, { retryAfterSeconds: faults.retryAfterSeconds })
      : HttpResponse.json({
          evidence_id: String(params.evidence_id),
          url: 'https://evidencia.example/clip-sintetico.mp4',
          expires_at: new Date(now() + 300_000).toISOString(),
        }),
  ),
  at('upload_grant_loop_actions__action_id__attachments_upload_grant_post', () =>
    faults.storageDown ? apiError('storage_unavailable', 503) : undefined,
  ),
];

/** FS-APP-05: cada URL emitida es distinta y vence a los `evidenceUrlSeconds` del reloj. */
const expiringEvidenceUrl: HttpHandler[] = [
  at('read_url_evidence__evidence_id__read_url_post', ({ params }) => {
    const id = String(params.evidence_id);
    const url = `https://evidencia.example/${id}.mp4?firma=${String(faults.issuedUrls.length + 1)}`;
    faults.issuedUrls.push(url);
    return HttpResponse.json({
      evidence_id: id,
      url,
      expires_at: new Date(now() + faults.evidenceUrlSeconds * 1000).toISOString(),
    });
  }),
];

/** FS-APP-06: con la sesión vencida, toda ruta con sesión responde `unauthenticated`. */
const sessionExpired: HttpHandler[] = [
  at('login_auth_login_post', () => {
    // Con segundo factor, la sesión solo se completa con el código (como la cuenta del mundo).
    if (faults.secondFactor) return HttpResponse.json({ status: 'second_factor_required' });
    faults.sessionExpired = false;
    return HttpResponse.json({ status: 'authenticated' });
  }),
  at('second_factor_auth_second_factor_post', () => {
    faults.sessionExpired = false;
    return HttpResponse.json({ status: 'authenticated' });
  }),
  http.all('*', ({ request }) => {
    const { pathname } = new URL(request.url);
    if (!faults.sessionExpired || pathname === '/health/live' || pathname.startsWith('/auth/')) {
      return undefined;
    }
    return apiError('unauthenticated', 401);
  }),
];

/** FS-APP-07: con el aviso pendiente, toda ruta con sesión responde `privacy_notice_required`. */
const privacyNotice: HttpHandler[] = [
  at('accept_notice_privacy_notice_accept_post', async ({ request }) => {
    const body = (await request.json()) as { notice_version?: unknown };
    if (body.notice_version !== faults.noticeVersion) return apiError('invalid_request', 400);
    faults.noticePending = false;
    return HttpResponse.json({ notice_version: faults.noticeVersion, newly_accepted: true });
  }),
  http.all('*', ({ request }) => {
    const { pathname } = new URL(request.url);
    if (!faults.noticePending || pathname === '/health/live' || pathname.startsWith('/auth/')) {
      return undefined;
    }
    if (pathname === '/privacy-notice/accept') return undefined;
    return apiError('privacy_notice_required', 403);
  }),
];

/**
 * FS-APP-08: tras el despliegue, cada respuesta de la API anuncia `apiVersion` en su cabecera;
 * la porción antigua la simula la prueba con una carga de código que falla.
 */
const newVersion: HttpHandler[] = [
  at('list_notifications_notifications_get', () =>
    HttpResponse.json(
      { items: [], next_after: null },
      {
        headers: faults.apiVersion === null ? {} : { 'X-Vigia-Api-Version': faults.apiVersion },
      },
    ),
  ),
];

export default {
  fs_app_base: base,
  fs_app_01_platform_timeout: platformTimeout,
  fs_app_02_write_unavailable: writeUnavailable,
  fs_app_03_rate_limited: rateLimited,
  fs_app_04_storage_unavailable: storageUnavailable,
  fs_app_05_expiring_evidence_url: expiringEvidenceUrl,
  fs_app_06_session_expired: sessionExpired,
  fs_app_07_privacy_notice: privacyNotice,
  fs_app_08_new_version: newVersion,
};
