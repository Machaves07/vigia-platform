/**
 * UI-09 `GlobalErrorHandler` (LC-APP-04, BR-APP-76, NFR-APP-18 y 30, PAT-APP-RES-07): fallo
 * cerrado ante cualquier excepción no controlada.
 *
 * - Cuatro vías de captura: el `ErrorBoundary` de las rutas (`RouteFailure`), el límite de error
 *   por encima del enrutador (`AppErrorBoundary`) y los eventos `error` y `unhandledrejection` de
 *   la ventana (`installGlobalErrorHandler`), que dejan el fallo en `GlobalErrors` para que el
 *   armazón lo pinte.
 * - La pantalla genérica «algo falló» sustituye entera la pantalla afectada: nunca quedan a la
 *   vista datos parciales como si fueran completos. Muestra el `correlation_id` copiable si el
 *   fallo es una respuesta de la API que lo trae (BR-APP-75) y ningún detalle interno (ni mensaje,
 *   ni pila, ni código).
 * - Descarta el estado de la pantalla afectada: sus consultas salen del `QueryClient` y su
 *   borrador, que vive en el estado de la pantalla, se pierde al desmontarla.
 * - Nada se escribe en la consola, en ningún modo (NFR-APP-18): React recibe `rootErrorOptions`
 *   (sin ellas escribe cada error capturado con `console.error`), React Router recibe
 *   `ignoreRouterError` y los eventos de la ventana se cancelan con `preventDefault`, que es lo
 *   que evita el informe del navegador.
 */
import { Component, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Link, matchPath, useLocation, useRouteError } from 'react-router';

import type { ScreenDescriptor } from '@/app/descriptor';
import { correlationOf } from '@/components/NoticeStack/notice';
import { isPortError } from '@/ports/shared';
import type { Screen } from '@/state/query';
import { text } from '@/texts';

import styles from './AppShell.module.css';

/** Un fallo no controlado y la ruta en la que ocurrió. */
interface GlobalFailure {
  readonly error: unknown;
  readonly route: string;
}

export interface GlobalErrors {
  readonly getSnapshot: () => GlobalFailure | null;
  readonly subscribe: (listener: () => void) => () => void;
  report(error: unknown, route: string): void;
  reset(): void;
}

export function createGlobalErrors(): GlobalErrors {
  let failure: GlobalFailure | null = null;
  const listeners = new Set<() => void>();
  const publish = (next: GlobalFailure | null): void => {
    failure = next;
    for (const listener of listeners) listener();
  };
  return {
    getSnapshot: () => failure,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    report(error, route) {
      // El primer fallo manda: los que provoca en cascada no cambian la pantalla.
      if (failure === null) publish({ error, route });
    },
    reset() {
      if (failure !== null) publish(null);
    },
  };
}

export function useGlobalFailure(errors: GlobalErrors): GlobalFailure | null {
  return useSyncExternalStore(errors.subscribe, errors.getSnapshot, errors.getSnapshot);
}

/** Una cancelación (cambio de pantalla, consulta descartada) no es un fallo. */
function isCancellation(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true;
  return isPortError(error) && error.failure.kind === 'aborted';
}

/**
 * Escucha `error` y `unhandledrejection` en `target`: cada fallo queda en `errors` con la ruta
 * actual y el evento se cancela, así que el navegador no lo escribe en la consola. Devuelve la
 * función que deja de escuchar.
 */
export function installGlobalErrorHandler(
  target: Pick<Window, 'addEventListener' | 'removeEventListener'>,
  errors: GlobalErrors,
  currentRoute: () => string,
): () => void {
  const onError = (event: ErrorEvent): void => {
    event.preventDefault();
    if (isCancellation(event.error)) return;
    errors.report(event.error ?? event.message, currentRoute());
  };
  const onRejection = (event: PromiseRejectionEvent): void => {
    event.preventDefault();
    if (isCancellation(event.reason)) return;
    errors.report(event.reason, currentRoute());
  };
  target.addEventListener('error', onError);
  target.addEventListener('unhandledrejection', onRejection);
  return () => {
    target.removeEventListener('error', onError);
    target.removeEventListener('unhandledrejection', onRejection);
  };
}

const IGNORE = (): void => undefined;

/**
 * Opciones de `createRoot`: los errores que captura un límite ya se pintan con la pantalla
 * genérica y ninguno se escribe en la consola.
 */
export const rootErrorOptions = Object.freeze({
  onCaughtError: IGNORE,
  onUncaughtError: IGNORE,
  onRecoverableError: IGNORE,
});

/** `onError` de `RouterProvider`: el `ErrorBoundary` de la ruta ya pinta el fallo. */
export const ignoreRouterError: (error: unknown) => void = IGNORE;

/** `correlation_id` de una respuesta de error de la API; `null` para cualquier otro fallo. */
export function failureCorrelation(error: unknown): string | null {
  if (!isPortError(error) || error.failure.kind !== 'http') return null;
  return correlationOf({ id: 'global', error: error.failure.error });
}

/** Clave de la caché (`scrNN`) de una pantalla (`SCR-NN`). */
function cacheScreen(screen: ScreenDescriptor): Screen {
  return `scr${screen.screen_id.slice(4)}` as Screen;
}

/**
 * Pantallas de la caché que pinta `pathname`: la de su grupo de rutas y, si el grupo es un panel
 * de inicio, también `home`.
 */
export function screensAt(pathname: string, screens: readonly ScreenDescriptor[]): Screen[] {
  for (const screen of screens) {
    for (const group of screen.route_groups) {
      if (!group.paths.some((path) => matchPath(path, pathname) !== null)) continue;
      return screen.home_group === group.id ? [cacheScreen(screen), 'home'] : [cacheScreen(screen)];
    }
  }
  return [];
}

/** Lo que se usa de `QueryClient` para descartar consultas. */
interface QueryDiscard {
  removeQueries(filters: { readonly queryKey: readonly [Screen] }): void;
}

/** Saca del `QueryClient` todas las consultas de la pantalla que pinta `pathname`. */
function discardScreenQueries(
  pathname: string,
  screens: readonly ScreenDescriptor[],
  client: QueryDiscard,
): void {
  for (const screen of screensAt(pathname, screens)) client.removeQueries({ queryKey: [screen] });
}

/** El `correlation_id` con la indicación de reporte y «copiar» (BR-APP-75). */
export function CorrelationCopy({ value }: { readonly value: string }): ReactNode {
  const [copied, setCopied] = useState(false);
  return (
    <p className={styles.correlation} data-part="correlation">
      <span>{text('app_shell.error.correlation')}: </span>
      <code data-part="correlation_id">{value}</code>{' '}
      <button
        type="button"
        className={styles.button}
        onClick={() => {
          navigator.clipboard.writeText(value).then(
            () => {
              setCopied(true);
            },
            () => {
              // Sin portapapeles el código sigue a la vista para copiarlo a mano.
            },
          );
        }}
      >
        {text('app_shell.error.copy')}
      </button>
      <span role="status" className={styles.copied}>
        {copied ? text('app_shell.error.copied') : ''}
      </span>
    </p>
  );
}

/** La pantalla genérica «algo falló», sin detalles internos. */
function GenericFailure({
  correlationId,
  home,
}: {
  readonly correlationId: string | null;
  readonly home: ReactNode;
}): ReactNode {
  return (
    <main className={styles.page}>
      <div className={styles.failure} role="alert" data-text-key="app_shell.error.title">
        <h1 className={styles.failureTitle}>{text('app_shell.error.title')}</h1>
        <p>{text('app_shell.error.body')}</p>
        {correlationId === null ? null : <CorrelationCopy value={correlationId} />}
        {home}
      </div>
    </main>
  );
}

/**
 * La pantalla genérica dentro del enrutador: al montarse descarta las consultas de la pantalla
 * afectada, que ya se desmontó.
 */
export function ScreenFailure({
  error,
  screens,
  client,
}: {
  readonly error: unknown;
  readonly screens: readonly ScreenDescriptor[];
  readonly client: QueryDiscard;
}): ReactNode {
  const { pathname } = useLocation();
  useEffect(() => {
    discardScreenQueries(pathname, screens, client);
  }, [pathname, screens, client]);
  return (
    <GenericFailure
      correlationId={failureCorrelation(error)}
      home={
        <Link className={styles.link} to="/">
          {text('app_shell.error.home')}
        </Link>
      }
    />
  );
}

/** `ErrorBoundary` de una ruta: lo que lanzó la pantalla, con su descarte. */
export function RouteFailure({
  screens,
  client,
}: {
  readonly screens: readonly ScreenDescriptor[];
  readonly client: QueryDiscard;
}): ReactNode {
  const error = useRouteError();
  return <ScreenFailure error={error} screens={screens} client={client} />;
}

/**
 * Último límite, por encima del enrutador y de los proveedores: si algo falla fuera de una ruta,
 * pinta la pantalla genérica sin depender de ellos. Volver al inicio carga la aplicación de nuevo,
 * sin nada del estado anterior.
 */
export class AppErrorBoundary extends Component<
  { readonly children?: ReactNode },
  { readonly error: unknown; readonly failed: boolean }
> {
  override state = { error: null as unknown, failed: false };

  static getDerivedStateFromError(error: unknown) {
    return { error, failed: true };
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <GenericFailure
        correlationId={failureCorrelation(this.state.error)}
        home={
          <a className={styles.link} href="/">
            {text('app_shell.error.home')}
          </a>
        }
      />
    );
  }
}
