/**
 * Almacén de la sesión (LC-APP-06): el reductor `UiSession` conectado al `Clock`, al puerto de
 * sesión y a `ports.base`.
 *
 * - Un solo temporizador del `Clock`, programado en el siguiente instante en que el reductor
 *   cambia (aviso de los 28 min, aviso absoluto, vencimiento): los avisos salen a su hora exacta
 *   por `onNotice`, una vez por ventana; `tick()` reevalúa al volver a la pestaña.
 * - `transportHooks` es lo que el arranque pasa a `createTransport`: cada respuesta autenticada
 *   reinicia la ventana; `unauthenticated` bloquea guardando la ruta y `privacy_notice_required`
 *   pasa a `privacy_notice_pending`. La escritura que recibió `unauthenticated` no se guarda ni
 *   se reenvía: la persona la repite al volver (BR-APP-16, PAT-APP-RES-05).
 * - Borradores (`FormDraft`, NFR-APP-38): solo en memoria. Una pantalla que se desmonta con la
 *   sesión perdida deja aquí su borrador; al volver a la misma ruta tras reautenticar la misma
 *   persona, lo recupera. Otra persona, otra ruta o el cierre de sesión lo descartan.
 * - `logout()`: `POST /auth/logout` y, aunque falle, `clearAll()` de la caché, borrado de
 *   `UiPreferences` (`state.scope`, inyectado), del almacenamiento del navegador y de todo el
 *   estado en memoria; vuelta a `/login` (BR-APP-17, BR-APP-19).
 */
import type { MeResponse } from '@/ports/shared';

import type { Clock, TimerHandle } from '../clock';
import { formDraftReducer, type FormDraft } from '../reducers/formDraft';

import { principalFromMe, type Principal } from './principal';
import {
  initialSession,
  isAuthenticated,
  nextDeadline,
  sessionTransition,
  type SessionNotice,
  type UiSession,
  type UiSessionEvent,
} from './reducer';

export const LOGIN_ROUTE = '/login';

/** Lo que el almacén usa del puerto de sesión (`sessionPort`). */
interface SessionPortLike {
  readonly me: { read(params?: undefined, signal?: AbortSignal): Promise<MeResponse> };
  readonly logout: { write(): Promise<unknown> };
}

export interface SessionStoreOptions {
  readonly clock: Clock;
  readonly port: SessionPortLike;
  /** `state.query`: vacía la caché. */
  readonly cache: { clearAll(): void };
  /** `state.scope` (VIG-301): borra `UiPreferences` y la selección en memoria. */
  readonly clearPreferences: () => void;
  /** Ruta actual (`pathname` y `search`). */
  readonly currentRoute: () => string;
  /** Navegación del enrutador. */
  readonly navigate: (route: string) => void;
  /** Almacenamiento del navegador que el cierre deja vacío; por omisión el de `window`. */
  readonly storages?: readonly Pick<Storage, 'clear'>[];
  /** Avisos de vencimiento (UI-05, VIG-320): cada uno llega una sola vez por ventana. */
  readonly onNotice?: (notice: SessionNotice) => void;
}

export interface SessionStore {
  /** Funciones sueltas (sin `this`): `useSyncExternalStore` las recibe tal cual. */
  readonly getSnapshot: () => UiSession;
  readonly subscribe: (listener: () => void) => () => void;
  /** Para `createTransport` (`onAuthenticatedResponse`, `onSessionEvent`). */
  readonly transportHooks: {
    readonly onAuthenticatedResponse: () => void;
    readonly onSessionEvent: (event: 'unauthenticated' | 'privacy_notice_required') => void;
  };
  /** `GET /me`: tras iniciar sesión, reautenticar, aceptar el aviso o «seguir trabajando». */
  load(signal?: AbortSignal): Promise<Principal>;
  /** «Seguir trabajando» del aviso de inactividad (BR-APP-14). */
  continueWorking(): Promise<void>;
  /**
   * Vuelve a evaluar los vencimientos con la hora del `Clock` (al volver a la pestaña: el
   * navegador retrasa los temporizadores de una pestaña oculta).
   */
  tick(): void;
  /** El inicio de sesión pide el segundo factor (IF-01). */
  secondFactorRequired(enrollmentRequired: boolean): void;
  /** La ruta guardada, que deja de estarlo; `null` si no hay. */
  takeReturnRoute(): string | null;
  /** Una pantalla que se desmonta deja su borrador; solo se guarda con la sesión perdida. */
  keepDraft(draft: FormDraft): void;
  /** El borrador guardado de este formulario en la ruta actual, ya restaurado; o `null`. */
  takeDraft(screenId: string, formId: string): FormDraft | null;
  logout(): Promise<void>;
  /** Cancela el temporizador. */
  dispose(): void;
}

interface Kept {
  readonly route: string;
  readonly draft: FormDraft;
}

function browserStorages(): readonly Pick<Storage, 'clear'>[] {
  if (typeof window === 'undefined') return [];
  return [window.localStorage, window.sessionStorage];
}

export function createSessionStore(options: SessionStoreOptions): SessionStore {
  const { clock, port } = options;
  let session = initialSession;
  let timer: TimerHandle | null = null;
  const listeners = new Set<() => void>();
  const drafts = new Map<string, Kept>();
  const draftKey = (screenId: string, formId: string) => JSON.stringify([screenId, formId]);

  const tick = () => {
    dispatch({ type: 'tick', nowMs: clock.now(), route: options.currentRoute() });
  };

  const schedule = () => {
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
    const at = nextDeadline(session);
    if (at === null) return;
    timer = clock.setTimeout(
      () => {
        timer = null;
        tick();
      },
      Math.max(0, at - clock.now()),
    );
  };

  function dispatch(event: UiSessionEvent): void {
    const previous = session;
    const transition = sessionTransition(session, event);
    session = transition.session;
    for (const notice of transition.notices) options.onNotice?.(notice);
    if (session === previous) return;
    // Reautenticó otra persona: ningún borrador de la anterior sobrevive.
    if (
      event.type === 'principal_loaded' &&
      previous.locked_user_id !== null &&
      previous.locked_user_id !== event.principal.user_id
    ) {
      drafts.clear();
    }
    schedule();
    for (const listener of listeners) listener();
  }

  const load = async (signal?: AbortSignal): Promise<Principal> => {
    const me = await port.me.read(undefined, signal);
    const principal = principalFromMe(me, clock.now());
    dispatch({ type: 'principal_loaded', principal, nowMs: principal.fetched_at });
    return principal;
  };

  return {
    getSnapshot: () => session,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    transportHooks: {
      onAuthenticatedResponse: () => {
        dispatch({ type: 'response', nowMs: clock.now() });
      },
      onSessionEvent: (event) => {
        dispatch({ type: event, route: options.currentRoute() });
      },
    },
    load,
    async continueWorking() {
      await load();
    },
    tick,
    secondFactorRequired(enrollmentRequired) {
      dispatch({ type: 'second_factor_required', enrollment_required: enrollmentRequired });
    },
    takeReturnRoute() {
      const route = session.return_route;
      if (route !== null) dispatch({ type: 'return_route_consumed' });
      return route;
    },
    keepDraft(draft) {
      // Con la sesión en uso el borrador es de la pantalla; sin sesión no hay nada que conservar.
      if (isAuthenticated(session.status) || session.status === 'anonymous') return;
      drafts.set(draftKey(draft.screenId, draft.formId), {
        route: session.return_route ?? options.currentRoute(),
        draft: formDraftReducer(draft, { type: 'lock' }),
      });
    },
    takeDraft(screenId, formId) {
      const key = draftKey(screenId, formId);
      const kept = drafts.get(key);
      if (kept === undefined || !isAuthenticated(session.status)) return null;
      drafts.delete(key);
      if (kept.route !== options.currentRoute()) return null;
      return formDraftReducer(kept.draft, { type: 'restore' });
    },
    async logout() {
      try {
        await port.logout.write();
      } catch {
        // Sin respuesta del servidor el cierre local sigue: la sesión vencerá en el servidor.
      } finally {
        options.cache.clearAll();
        options.clearPreferences();
        for (const storage of options.storages ?? browserStorages()) storage.clear();
        drafts.clear();
        dispatch({ type: 'logout' });
        options.navigate(LOGIN_ROUTE);
      }
    },
    dispose() {
      if (timer !== null) clock.clearTimeout(timer);
      timer = null;
    },
  };
}
