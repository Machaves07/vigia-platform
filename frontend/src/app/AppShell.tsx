/**
 * `app.shell` (LC-APP-02): la aplicación que la persona ve.
 *
 * - `App` monta los proveedores (sesión, caché de consultas, servicios del armazón) bajo el último
 *   límite de error, ejecuta el arranque (`bootShell`: `GET /me` y `GET /hierarchy` en paralelo) y,
 *   solo si `GET /me` no falló, el enrutador. Mientras tanto, el esqueleto; si `GET /me` falla,
 *   un aviso con «repetir» y nada más: ni navegación ni pantalla parcial (PAT-APP-RES-07).
 * - Rutas de diseño sobre el árbol de `createAppRoutes` (nota de VIG-321): el puente con el
 *   enrutador, `SessionGate` de SCR-01 (segundo factor y aviso de tratamiento en cualquier ruta) y
 *   `ShellLayout`. `SessionProvider` envuelve también las rutas públicas.
 * - `ShellLayout` pinta UI-01 `AppShell` (UI-02 cabecera, UI-07 menú, avisos permanentes UI-04,
 *   UI-05, UI-06 y el de versión nueva) alrededor de la pantalla de una sesión en uso; con la sesión
 *   perdida, la pantalla bloqueada de UI-05; sin sesión, solo la ruta pública.
 * - UI-09 (`GlobalErrorHandler`): el `ErrorBoundary` de cada nivel de rutas y los fallos de la
 *   ventana sustituyen la pantalla por la genérica y descartan sus consultas.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ComponentType,
  type ReactNode,
} from 'react';
import {
  createBrowserRouter,
  Outlet,
  useLocation,
  useNavigate,
  type RouteObject,
} from 'react-router';

import type { ScreenDescriptor } from '@/app/descriptor';
import { shellNavigation } from '@/app/shellNavigation';
import { AppRouter } from '@/app/router';
import { SCREENS } from '@/app/screens';
import { ConcessionBanner } from '@/components/ConcessionBanner/ConcessionBanner';
import { SessionGate } from '@/pages/scr01/SessionGate';
import { ConcessionProvider } from '@/state/concession';
import { ScreenQueryProvider } from '@/state/query';
import { useScope, type ScopeSnapshot } from '@/state/scope';
import { isAuthenticated, needsReauth, SessionProvider, useUiSession } from '@/state/session';
import type { Principal } from '@/state/session/principal';
import { text } from '@/texts';

import styles from './AppShell.module.css';
import { bootShell, type AppServices, type BootOutcome } from './bootstrap';
import { ConnectionNotice } from './ConnectionNotice';
import {
  AppErrorBoundary,
  CorrelationCopy,
  ignoreRouterError,
  RouteFailure,
  ScreenFailure,
  useGlobalFailure,
} from './GlobalErrorHandler';
import { Header } from './Header';
import { NavMenu } from './NavMenu';
import { SessionLock, SessionNotice } from './SessionNotice';
import type { AppVersion } from './version';

type DataRouter = ReturnType<typeof createBrowserRouter>;

interface ShellContextValue {
  readonly services: AppServices;
  readonly screens: readonly ScreenDescriptor[];
}

const ShellContext = createContext<ShellContextValue | null>(null);

function useShell(): ShellContextValue {
  const value = useContext(ShellContext);
  if (value === null) throw new Error('Falta App por encima del armazón.');
  return value;
}

const noSubscription = () => () => undefined;
const noPlants = () => null;

/**
 * Zona horaria de la planta para pantallas que no son de una planta (notificaciones, integridad;
 * VIG-366): la de la planta elegida en el selector; con «todas», la que comparten las plantas del
 * alcance. Solo si difieren (o fuera del armazón), UTC, que la hora siempre nombra. Una pantalla
 * de una planta (admisiones) pasa `plantId`: la de esa planta, si es del alcance.
 */
export function usePlantTimeZone(plantId?: string): string {
  const scope = useContext(ShellContext)?.services.scope ?? null;
  const snapshot = useSyncExternalStore(
    scope?.subscribe ?? noSubscription,
    scope?.getSnapshot ?? noPlants,
  );
  return scopeTimeZone(snapshot, plantId);
}

function scopeTimeZone(
  snapshot: Pick<ScopeSnapshot, 'plantId' | 'availablePlants'> | null,
  plantId?: string,
): string {
  const plants = snapshot?.availablePlants ?? [];
  const chosen = plants.find((plant) => plant.plant_id === (plantId ?? snapshot?.plantId));
  if (chosen !== undefined) return chosen.timezone;
  const zones = new Set(plants.map((plant) => plant.timezone));
  return zones.size === 1 ? (plants[0]?.timezone ?? 'UTC') : 'UTC';
}

/** Esqueleto del armazón mientras llega `GET /me` o la porción de la primera ruta. */
function AppSkeleton(): ReactNode {
  return (
    <main className={styles.skeleton}>
      <h1 className={styles.skeletonTitle}>{text('common.app_name')}</h1>
      <p className={styles.skeletonStatus} role="status">
        {text('common.loading')}
      </p>
    </main>
  );
}

/** `GET /me` falló: ni navegación ni pantalla; solo el motivo y «repetir». */
function BootFailure({
  correlationId,
  onRetry,
}: {
  readonly correlationId: string | null;
  readonly onRetry: () => void;
}): ReactNode {
  return (
    <main className={styles.skeleton}>
      <h1 className={styles.skeletonTitle}>{text('common.app_name')}</h1>
      <div className={styles.failure} role="alert" data-text-key="app_shell.boot.failed">
        <p>{text('app_shell.boot.failed')}</p>
        {correlationId === null ? null : <CorrelationCopy value={correlationId} />}
        <button type="button" className={styles.button} onClick={onRetry}>
          {text('app_shell.boot.retry')}
        </button>
      </div>
    </main>
  );
}

/** Aviso permanente de versión nueva (LC-APP-05): las escrituras quedan bloqueadas hasta recargar. */
function VersionNotice({ version }: { readonly version: AppVersion }): ReactNode {
  const subscribe = useCallback((listener: () => void) => version.subscribe(listener), [version]);
  const snapshot = useSyncExternalStore(subscribe, version.getSnapshot);
  const { pathname, search, hash } = useLocation();
  if (!snapshot.outdated) return null;
  return (
    <section
      className={styles.notice}
      aria-label={text('app_shell.version.label')}
      data-part="version_notice"
    >
      <p className={styles.noticeText} role="status" data-text-key="version.outdated">
        {text('version.outdated')}
      </p>
      <button
        type="button"
        className={styles.button}
        onClick={() => {
          version.requestReload({ route: `${pathname}${search}${hash}`, draft: null });
        }}
      >
        {text('app_shell.version.reload')}
      </button>
    </section>
  );
}

/** UI-01: cabecera, avisos permanentes, menú y el área de la pantalla. */
function AppShell({
  principal,
  services,
  screens,
  children,
}: {
  readonly principal: Principal;
  readonly services: AppServices;
  readonly screens: readonly ScreenDescriptor[];
  readonly children?: ReactNode;
}): ReactNode {
  const navigation = useMemo(() => shellNavigation(principal, screens), [principal, screens]);
  const timeZone = scopeTimeZone(useScope(services.scope));
  return (
    <div className={styles.shell}>
      <Header scope={services.scope} role={navigation.home?.role ?? null} />
      <div className={styles.notices}>
        <SessionNotice />
        <ConnectionNotice
          connection={services.connection}
          cache={services.cache}
          timeZone={timeZone}
        />
        <VersionNotice version={services.version} />
        <ConcessionBanner store={services.concession} timeZone={timeZone} />
      </div>
      <div className={styles.body}>
        <NavMenu navigation={navigation} screens={screens} />
        <div className={styles.content}>
          <div className={styles.contentInner}>{children}</div>
        </div>
      </div>
    </div>
  );
}

/** Ruta de diseño del armazón: decide qué rodea a la pantalla según la sesión. */
function ShellLayout(): ReactNode {
  const { services, screens } = useShell();
  const session = useUiSession();
  const { pathname, search } = useLocation();
  const route = `${pathname}${search}`;
  const failure = useGlobalFailure(services.errors);
  // Un fallo de la ventana vale para la ruta en que ocurrió: navegar sale de la pantalla genérica.
  const stale = failure !== null && failure.route !== route;
  useEffect(() => {
    if (stale) services.errors.reset();
  }, [stale, services]);
  if (needsReauth(session.status)) return <SessionLock status={session.status} />;
  const content =
    failure !== null && !stale ? (
      <ScreenFailure error={failure.error} screens={screens} client={services.cache.client} />
    ) : (
      <Outlet />
    );
  const { principal } = session;
  if (principal === null || !isAuthenticated(session.status)) return content;
  return (
    <AppShell principal={principal} services={services} screens={screens}>
      {content}
    </AppShell>
  );
}

/** `ErrorBoundary` de las rutas: la pantalla genérica con el descarte de la pantalla afectada. */
function RouteErrorScreen(): ReactNode {
  const { services, screens } = useShell();
  return <RouteFailure screens={screens} client={services.cache.client} />;
}

/** Registra el enrutador montado en los servicios: la sesión y la concesión navegan con él. */
function RouterBridgeLayout(): ReactNode {
  const { services } = useShell();
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const route = useRef(`${pathname}${search}`);
  useEffect(() => {
    route.current = `${pathname}${search}`;
    services.routeChanged(route.current);
  }, [services, pathname, search]);
  useEffect(() => {
    services.attachRouter({
      route: () => route.current,
      navigate: (to) => {
        void navigate(to);
      },
    });
    return () => {
      services.attachRouter(null);
    };
  }, [services, navigate]);
  return <Outlet />;
}

/** Las rutas de diseño del armazón por encima del árbol de `createAppRoutes`. */
function shellRoutes(routes: RouteObject[]): RouteObject[] {
  return [
    {
      Component: RouterBridgeLayout,
      ErrorBoundary: RouteErrorScreen,
      children: [
        {
          Component: SessionGate,
          children: [
            {
              Component: ShellLayout,
              children: routes.map((route) => ({ ...route, ErrorBoundary: RouteErrorScreen })),
            },
          ],
        },
      ],
    },
  ];
}

function Boot({
  fallback: Fallback,
  createRouter,
}: {
  readonly fallback: ComponentType;
  readonly createRouter?: (routes: RouteObject[]) => DataRouter;
}): ReactNode {
  const { services, screens } = useShell();
  const session = useUiSession();
  const [outcome, setOutcome] = useState<BootOutcome | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    void bootShell(services, controller.signal).then((result) => {
      if (live) setOutcome(result);
    });
    return () => {
      live = false;
      controller.abort();
    };
  }, [services, attempt]);
  // Cada persona que entra (también otra al reautenticar) llena el selector con su alcance; la
  // jerarquía que ya trajo el arranque no se vuelve a pedir.
  const userId = session.principal?.user_id ?? null;
  const lastUser = useRef<string | null>(null);
  useEffect(() => {
    if (userId === null) return;
    const otherPerson = lastUser.current !== null && lastUser.current !== userId;
    lastUser.current = userId;
    if (otherPerson || services.scope.getSnapshot().availablePlants === null) {
      void services.loadHierarchy();
    }
  }, [services, userId]);
  const retry = useCallback(() => {
    setOutcome(null);
    setAttempt((value) => value + 1);
  }, []);
  if (outcome === null) return <Fallback />;
  if (outcome.kind === 'failed') {
    return <BootFailure correlationId={outcome.correlationId} onRetry={retry} />;
  }
  return (
    <AppRouter
      principal={session.principal}
      fallback={Fallback}
      screens={screens}
      layout={shellRoutes}
      onError={ignoreRouterError}
      {...(createRouter === undefined ? {} : { createRouter })}
    />
  );
}

export interface AppProps {
  readonly services: AppServices;
  /** Tabla de pantallas; por omisión `SCREENS` (las pruebas pasan pantallas sintéticas). */
  readonly screens?: readonly ScreenDescriptor[];
  readonly fallback?: ComponentType;
  /** Enrutador del árbol; por omisión `createBrowserRouter`. */
  readonly createRouter?: (routes: RouteObject[]) => DataRouter;
}

export function App({
  services,
  screens = SCREENS,
  fallback = AppSkeleton,
  createRouter,
}: AppProps): ReactNode {
  const shell = useMemo(() => ({ services, screens }), [services, screens]);
  return (
    <AppErrorBoundary>
      <ShellContext.Provider value={shell}>
        <SessionProvider store={services.session}>
          <ScreenQueryProvider cache={services.cache}>
            <ConcessionProvider store={services.concession}>
              <Boot fallback={fallback} {...(createRouter === undefined ? {} : { createRouter })} />
            </ConcessionProvider>
          </ScreenQueryProvider>
        </SessionProvider>
      </ShellContext.Provider>
    </AppErrorBoundary>
  );
}
