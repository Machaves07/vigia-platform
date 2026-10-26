/**
 * Arranque, paso 3 de PAT-APP-REN-01 (LC-APP-01 y 02, PAT-APP-RES-07): los servicios de la
 * aplicación conectados entre sí y la carga de `GET /me` y `GET /hierarchy` en paralelo.
 *
 * - `createAppServices` crea una vez el reloj, los circuitos (`state.connection`), la caché de
 *   consultas con sus puertas (visibilidad, circuito `platform` y actividad), `UiPreferences`, la
 *   selección de planta, la concesión, la sesión, los fallos globales y el transporte con todos
 *   sus ganchos (sesión, concesión, versión). El cierre de sesión vacía además `UiPreferences`, la
 *   selección y la concesión (nota de VIG-301): si solo se vaciara el almacenamiento, la siguiente
 *   escritura de la misma pestaña devolvería las preferencias de la persona anterior.
 * - `bootShell` pide `GET /me` y `GET /hierarchy` a la vez. Solo `GET /me` decide: sin él no hay
 *   navegación ni pantalla, tampoco parcial (`failed`); `unauthenticated` o
 *   `privacy_notice_required` dejan las rutas públicas y las puertas de sesión (`signed_out`).
 *   `GET /hierarchy` llena el selector de planta cuando llega; si falla, el selector se queda en
 *   «todas las de mi alcance», que es lo que el servidor acota de todos modos.
 * - La navegación, la pantalla de inicio y la precarga de su porción las construye `AppRouter`
 *   con el `Principal` (VIG-290).
 */
import { isPortError, type PortConnection } from '@/ports/base';
import { createPortConnection, createTransport, type Transport } from '@/ports/base/http';
import { sessionPort } from '@/ports/shared';
import { systemClock, type Clock } from '@/state/clock';
import { createConcessionStore, transportOptions, type ConcessionStore } from '@/state/concession';
import { createScreenQueryCache, refreshGates, type ScreenQueryCache } from '@/state/query';
import {
  createPreferencesStore,
  createScopeStore,
  plantsFromHierarchy,
  type ScopeStore,
} from '@/state/scope';
import type { PreferencesStorage } from '@/state/scope/preferences';
import { createActivityGate, createSessionStore } from '@/state/session';
import type { ActivityGate } from '@/state/session/activity';
import type { SessionStore } from '@/state/session/store';

import { createGlobalErrors, failureCorrelation, type GlobalErrors } from './GlobalErrorHandler';
import { appVersion, type AppVersion } from './version';

/** Panel del proveedor (SCR-02, grupo `provider`), al que se vuelve cuando termina una concesión. */
const PROVIDER_PANEL = '/provider/concessions';

/** Lo que los servicios usan del enrutador montado: dónde está y cómo navegar. */
interface RouterBridge {
  readonly route: () => string;
  readonly navigate: (route: string) => void;
}

export interface AppServices {
  readonly clock: Clock;
  readonly connection: PortConnection;
  readonly cache: ScreenQueryCache;
  readonly activity: ActivityGate;
  readonly scope: ScopeStore;
  readonly concession: ConcessionStore;
  readonly session: SessionStore;
  readonly version: AppVersion;
  readonly errors: GlobalErrors;
  readonly transport: Transport;
  /** El enrutador montado se registra aquí (`null` al desmontarse). */
  attachRouter(bridge: RouterBridge | null): void;
  /** El enrutador llegó a `route` (lo avisa el puente en cada cambio de ruta). */
  routeChanged(route: string): void;
  /** Ruta actual (`pathname` y `search`). */
  readonly currentRoute: () => string;
  /** `GET /hierarchy`: una sola petición en vuelo; nunca rechaza. */
  loadHierarchy(): Promise<void>;
  dispose(): void;
}

export interface AppServicesOptions {
  readonly clock?: Clock;
  /** Almacenamiento de `UiPreferences`; por omisión `localStorage`. */
  readonly storage?: PreferencesStorage | null;
  /** Origen de la API; por omisión, el de la página. */
  readonly origin?: string;
  readonly version?: AppVersion;
  readonly document?: Pick<Document, 'visibilityState'>;
  /** Dónde escucha la puerta de actividad; por omisión `window`. */
  readonly activityTarget?: EventTarget | null;
}

function browserStorage(): PreferencesStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    // Almacenamiento bloqueado: las preferencias viven solo en memoria.
    return null;
  }
}

function locationRoute(): string {
  const { pathname, search } = globalThis.location;
  return `${pathname}${search}`;
}

export function createAppServices(options: AppServicesOptions = {}): AppServices {
  const clock = options.clock ?? systemClock;
  const version = options.version ?? appVersion;
  const connection = createPortConnection(clock);
  const activity = createActivityGate({
    clock,
    ...(options.activityTarget === undefined ? {} : { target: options.activityTarget }),
  });
  const cache = createScreenQueryCache({
    clock,
    gates: refreshGates({
      document: options.document ?? document,
      connection,
      isActive: () => activity.isOpen(),
    }),
  });
  const preferences = createPreferencesStore(
    options.storage === undefined ? browserStorage() : options.storage,
  );
  const scope = createScopeStore(preferences);
  const errors = createGlobalErrors();

  let bridge: RouterBridge | null = null;
  // Navegación pedida que el enrutador aún no alcanzó. Si entretanto se reconstruye (otro
  // `Principal`, como al cerrar sesión), el enrutador nuevo arranca en la ruta anterior y la
  // completa al registrarse: el cierre de sesión acaba en `/login`.
  let pending: string | null = null;
  const currentRoute = (): string => bridge?.route() ?? locationRoute();
  const navigate = (route: string): void => {
    pending = route;
    bridge?.navigate(route);
  };

  // La sesión y la concesión se necesitan una a otra; la concesión lee la sesión en cada llamada.
  let sessionRef: SessionStore | null = null;
  const concession = createConcessionStore({
    clock,
    organizationKind: () => sessionRef?.getSnapshot().principal?.organization.kind ?? null,
    cache,
    onEnded: () => {
      navigate(PROVIDER_PANEL);
      // La identidad de GET /me anterior todavía refleja la concesión terminada.
      void sessionRef?.load().catch(() => undefined);
    },
  });

  // Cada cierre de sesión abre una generación nueva: una jerarquía pedida antes no se aplica.
  let generation = 0;
  const session = createSessionStore({
    clock,
    port: sessionPort,
    cache,
    clearPreferences: () => {
      generation += 1;
      preferences.clear();
      scope.reset();
      concession.reset();
    },
    currentRoute,
    navigate,
  });
  sessionRef = session;

  const transport = createTransport({
    clock,
    connection,
    version,
    ...transportOptions(concession),
    onSessionEvent: session.transportHooks.onSessionEvent,
    onAuthenticatedResponse: session.transportHooks.onAuthenticatedResponse,
    ...(options.origin === undefined ? {} : { origin: options.origin }),
  });

  let hierarchyInFlight: Promise<void> | null = null;
  const loadHierarchy = (): Promise<void> => {
    if (hierarchyInFlight !== null) return hierarchyInFlight;
    const requested = generation;
    hierarchyInFlight = sessionPort.hierarchy
      .read()
      .then(
        (view) => {
          // Una respuesta pedida antes de un cierre de sesión no llena el selector de la siguiente.
          if (requested !== generation) return;
          scope.setAvailablePlants(plantsFromHierarchy(view));
          concession.describeClient(view);
        },
        () => {
          // Sin jerarquía, el selector queda en «todas las de mi alcance».
        },
      )
      .finally(() => {
        hierarchyInFlight = null;
      });
    return hierarchyInFlight;
  };

  return {
    clock,
    connection,
    cache,
    activity,
    scope,
    concession,
    session,
    version,
    errors,
    transport,
    attachRouter(next) {
      bridge = next;
      if (next !== null && pending !== null && next.route() !== pending) next.navigate(pending);
    },
    routeChanged(route) {
      if (route === pending) pending = null;
    },
    currentRoute,
    loadHierarchy,
    dispose() {
      session.dispose();
      concession.dispose();
      activity.dispose();
      cache.dispose();
    },
  };
}

/** Resultado del paso 3 del arranque. */
export type BootOutcome =
  /** `GET /me` respondió: hay `Principal`, navegación y pantalla de inicio. */
  | { readonly kind: 'ready' }
  /** Sin sesión (o con el aviso de tratamiento pendiente): rutas públicas y puertas de SCR-01. */
  | { readonly kind: 'signed_out' }
  /** `GET /me` falló de otro modo: ninguna navegación ni pantalla, tampoco parcial. */
  | { readonly kind: 'failed'; readonly correlationId: string | null };

/** Rechazos de `GET /me` que significan «sin sesión utilizable», no un fallo. */
const SIGNED_OUT_CODES: ReadonlySet<string> = new Set([
  'unauthenticated',
  'privacy_notice_required',
]);

function bootFailure(error: unknown): BootOutcome {
  if (
    isPortError(error) &&
    error.failure.kind === 'http' &&
    SIGNED_OUT_CODES.has(error.failure.error.code)
  ) {
    return { kind: 'signed_out' };
  }
  return { kind: 'failed', correlationId: failureCorrelation(error) };
}

/** `GET /me` y `GET /hierarchy` en paralelo; decide `GET /me`. */
export async function bootShell(services: AppServices, signal?: AbortSignal): Promise<BootOutcome> {
  const hierarchy = services.loadHierarchy();
  try {
    await services.session.load(signal);
  } catch (error) {
    return bootFailure(error);
  }
  // La jerarquía no retrasa la primera pantalla: llena el selector cuando llega.
  void hierarchy;
  return { kind: 'ready' };
}
