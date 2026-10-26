/**
 * Ejemplos de `state.session` (VIG-300): transiciones de IF-04, `Principal` de `GET /me`, avisos
 * de inactividad y absoluto (BR-APP-14, 15), puerta de actividad (D-4), bloqueo con borrador y
 * ruta conservados sin reenviar la escritura (BR-APP-16, NFR-APP-38) y cierre que lo borra todo
 * (BR-APP-17, 19). La red es MSW; el tiempo, `FakeClock`.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { createElement, type ChangeEvent, type SyntheticEvent } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { createAppVersion } from '@/app/version';
import { currentTransport } from '@/ports/base/http';
import { sessionPort } from '@/ports/shared';
import { createScreenQueryCache, screenKey } from '@/state/query';
import {
  ABSOLUTE_WARNING_BEFORE_MS,
  ACTIVITY_GATE_MS,
  createActivityGate,
  createSessionStore,
  IDLE_TIMEOUT_MS,
  IDLE_WARNING_BEFORE_MS,
  initialSession,
  LOGIN_ROUTE,
  principalFromMe,
  SessionProvider,
  sessionReducer,
  useFormDraft,
  useSession,
  useUiSession,
  type SessionNotice,
  type SessionStoreOptions,
} from '@/state/session';
import type { SessionStore } from '@/state/session/store';

import { apiError, variant } from '../../msw/handlers';
import { server } from '../../setup/msw';
import { FakeClock } from '../../support/fakeClock';
import { meResponse, USER_A, USER_B } from '../../support/session';

const START = 1_800_000_000_000;
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const FORM_ROUTE = '/users?tab=invite';
/** Tope de las esperas de MSW y React: no es lo que se prueba (retro 15). */
const WAIT = { timeout: 10_000 };

afterEach(() => {
  cleanup();
  installTransport(null);
  localStorage.clear();
  sessionStorage.clear();
});

/** Almacén con red real (transporte de `ports.base` sobre MSW) y el puerto de sesión. */
function setup(extra: Partial<SessionStoreOptions> = {}) {
  const clock = new FakeClock(START);
  const notices: SessionNotice[] = [];
  const navigate = vi.fn<(route: string) => void>();
  const clearPreferences = vi.fn<() => void>();
  const route = { current: FORM_ROUTE };
  const queries = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const store = createSessionStore({
    clock,
    port: sessionPort,
    cache: queries,
    clearPreferences,
    currentRoute: () => route.current,
    navigate,
    onNotice: (notice) => notices.push(notice),
    ...extra,
  });
  installTransport(
    createTransport({
      clock,
      // La sesión sintética anuncia 1.0.0: controlador aislado por prueba, como su reloj.
      version: createAppVersion({ appVersion: '1.0.0', contractTag: 'v1.1.0', clock }),
      connection: createPortConnection(clock),
      devLog: () => undefined,
      ...store.transportHooks,
    }),
  );
  const me = (options: { userId?: string; absolute?: number } = {}) =>
    variant('me_me_get', () =>
      HttpResponse.json(
        meResponse({
          idleExpiresAt: clock.now() + IDLE_TIMEOUT_MS,
          absoluteExpiresAt: options.absolute ?? START + 12 * HOUR_MS,
          ...(options.userId === undefined ? {} : { userId: options.userId }),
        }),
      ),
    );
  return { clock, notices, navigate, clearPreferences, route, queries, store, me };
}

describe('Principal desde GET /me (BR-APP-01, domain-entities §2.2)', () => {
  it('copia effective_permissions tal cual, sin derivarlos de los roles', () => {
    const me = meResponse({
      idleExpiresAt: START + IDLE_TIMEOUT_MS,
      absoluteExpiresAt: START + 12 * HOUR_MS,
      permissions: ['findings.read'],
    });
    const principal = principalFromMe(me, START);
    // El rol es `administrator`, pero solo cuenta lo que el servidor dio.
    expect(principal.effective_permissions).toEqual(['findings.read']);
    expect(principal.idle_expires_at).toBe(START + IDLE_TIMEOUT_MS);
    expect(principal.absolute_expires_at).toBe(START + 12 * HOUR_MS);
    expect(principal.professional_license).toBeNull();
    expect(principal.second_factor_enrolled).toBe(true);
    expect(principal.fetched_at).toBe(START);
  });

  it('sin el aditivo de VIG-278, professional_license y second_factor_enrolled quedan ausentes', () => {
    const older: Record<string, unknown> = {
      ...meResponse({ idleExpiresAt: START, absoluteExpiresAt: START }),
    };
    Reflect.deleteProperty(older, 'professional_license');
    Reflect.deleteProperty(older, 'second_factor_enrolled');
    const principal = principalFromMe(older as never, START);
    expect(Object.hasOwn(principal, 'professional_license')).toBe(false);
    expect(Object.hasOwn(principal, 'second_factor_enrolled')).toBe(false);
  });

  it.each(['', 'mañana', '2026-10-24', '2026-10-24T10:00:00', '2026-13-45T99:99:99Z', 17])(
    'un vencimiento que no es un instante con zona (%s) hace fallar la lectura',
    (value) => {
      const me = { ...meResponse({ idleExpiresAt: START, absoluteExpiresAt: START }) };
      expect(() => principalFromMe({ ...me, idle_expires_at: value as string }, START)).toThrow(
        TypeError,
      );
      expect(() => principalFromMe({ ...me, absolute_expires_at: value as string }, START)).toThrow(
        TypeError,
      );
    },
  );

  it('GET /me sale sin parámetros: nunca role_in_use', async () => {
    const { store } = setup();
    const urls: string[] = [];
    server.use(
      variant('me_me_get', ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json(
          meResponse({
            idleExpiresAt: START + IDLE_TIMEOUT_MS,
            absoluteExpiresAt: START + HOUR_MS,
          }),
        );
      }),
    );
    await store.load();
    expect(urls).toHaveLength(1);
    const url = new URL(urls[0] ?? '');
    expect(url.pathname).toBe('/me');
    expect(url.search).toBe('');
  });
});

describe('transiciones de UiSession (IF-04)', () => {
  const principal = (userId = USER_A) =>
    principalFromMe(
      meResponse({
        idleExpiresAt: START + IDLE_TIMEOUT_MS,
        absoluteExpiresAt: START + 12 * HOUR_MS,
        userId,
      }),
      START,
    );

  it('anonymous → second_factor_pending (con inscripción obligatoria) → active', () => {
    let session = sessionReducer(initialSession, {
      type: 'second_factor_required',
      enrollment_required: true,
    });
    expect(session.status).toBe('second_factor_pending');
    expect(session.second_factor_enrollment_required).toBe(true);
    session = sessionReducer(session, {
      type: 'principal_loaded',
      principal: principal(),
      nowMs: START,
    });
    expect(session.status).toBe('active');
    expect(session.second_factor_enrollment_required).toBe(false);
  });

  it('privacy_notice_required → privacy_notice_pending con la ruta; aceptado, vuelve a active', () => {
    let session = sessionReducer(initialSession, {
      type: 'principal_loaded',
      principal: principal(),
      nowMs: START,
    });
    session = sessionReducer(session, { type: 'privacy_notice_required', route: '/inbox' });
    expect(session.status).toBe('privacy_notice_pending');
    expect(session.return_route).toBe('/inbox');
    session = sessionReducer(session, {
      type: 'principal_loaded',
      principal: principal(),
      nowMs: START + MINUTE_MS,
    });
    expect(session.status).toBe('active');
    expect(session.return_route).toBe('/inbox');
  });

  it('unauthenticated sin sesión no cambia nada; con sesión bloquea guardando la ruta', () => {
    expect(sessionReducer(initialSession, { type: 'unauthenticated', route: '/x' })).toBe(
      initialSession,
    );
    let session = sessionReducer(initialSession, {
      type: 'principal_loaded',
      principal: principal(),
      nowMs: START,
    });
    session = sessionReducer(session, { type: 'unauthenticated', route: '/inbox' });
    expect(session.status).toBe('locked_for_reauth');
    expect(session.return_route).toBe('/inbox');
    expect(session.locked_user_id).toBe(USER_A);
    // Un segundo rechazo desde otra pantalla no pisa la ruta guardada.
    session = sessionReducer(session, { type: 'unauthenticated', route: '/otra' });
    expect(session.return_route).toBe('/inbox');
    expect(sessionReducer(session, { type: 'logout' })).toBe(initialSession);
  });

  it('reautenticar otra persona descarta la ruta guardada', () => {
    let session = sessionReducer(initialSession, {
      type: 'principal_loaded',
      principal: principal(USER_A),
      nowMs: START,
    });
    session = sessionReducer(session, { type: 'unauthenticated', route: '/inbox' });
    session = sessionReducer(session, {
      type: 'principal_loaded',
      principal: principal(USER_B),
      nowMs: START + MINUTE_MS,
    });
    expect(session.status).toBe('active');
    expect(session.return_route).toBeNull();
  });
});

describe('aviso de inactividad a los 28 min (BR-APP-14)', () => {
  it('sale exactamente a los 28 min de la última respuesta, una vez; vence a los 30 min', async () => {
    const { clock, notices, store, me } = setup();
    server.use(me());
    await store.load();
    clock.advance(IDLE_TIMEOUT_MS - IDLE_WARNING_BEFORE_MS - 1);
    expect(store.getSnapshot().status).toBe('active');
    expect(notices).toEqual([]);
    clock.advance(1);
    expect(store.getSnapshot().status).toBe('expiring_soon');
    expect(notices).toEqual(['idle_warning']);
    store.tick();
    clock.advance(IDLE_WARNING_BEFORE_MS - 1);
    store.tick();
    expect(notices).toEqual(['idle_warning']);
    clock.advance(1);
    expect(store.getSnapshot()).toMatchObject({ status: 'expired_idle', return_route: FORM_ROUTE });
  });

  it('una respuesta a los 27 min reinicia la ventana: sin aviso hasta 28 min después de ella', async () => {
    const { clock, notices, store, me } = setup();
    server.use(me());
    await store.load();
    clock.advance(27 * MINUTE_MS);
    store.transportHooks.onAuthenticatedResponse();
    clock.advance(27 * MINUTE_MS);
    expect(notices).toEqual([]);
    clock.advance(MINUTE_MS);
    expect(notices).toEqual(['idle_warning']);
  });

  it('«seguir trabajando» ejecuta GET /me y abre una ventana nueva', async () => {
    const { clock, notices, store, me } = setup();
    let calls = 0;
    server.use(me());
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname === '/me') calls += 1;
    });
    await store.load();
    clock.advance(28 * MINUTE_MS);
    expect(store.getSnapshot().status).toBe('expiring_soon');
    await store.continueWorking();
    expect(calls).toBe(2);
    expect(store.getSnapshot().status).toBe('active');
    expect(store.getSnapshot().idle_deadline_at).toBe(START + 28 * MINUTE_MS + IDLE_TIMEOUT_MS);
    clock.advance(28 * MINUTE_MS);
    expect(notices).toEqual(['idle_warning', 'idle_warning']);
  });

  it('desde React: useUiSession refleja el aviso y «seguir trabajando» lo retira', async () => {
    const { clock, store, me } = setup();
    server.use(me());
    await store.load();
    function Banner() {
      const session = useSession();
      const { status } = useUiSession();
      return createElement(
        'div',
        null,
        createElement('output', { 'aria-label': 'estado' }, status),
        createElement(
          'button',
          {
            type: 'button',
            onClick: () => {
              void session.continueWorking();
            },
          },
          'Seguir trabajando',
        ),
      );
    }
    render(createElement(SessionProvider, { store }, createElement(Banner)));
    expect(screen.getByLabelText('estado').textContent).toBe('active');
    act(() => {
      clock.advance(28 * MINUTE_MS);
    });
    expect(screen.getByLabelText('estado').textContent).toBe('expiring_soon');
    fireEvent.click(screen.getByRole('button', { name: 'Seguir trabajando' }));
    await vi.waitFor(() => {
      expect(screen.getByLabelText('estado').textContent).toBe('active');
    }, WAIT);
  });

  it('el vencimiento lo marca idle_expires_at del servidor, no una marca propia', async () => {
    const { clock, notices, store } = setup();
    server.use(
      variant('me_me_get', () =>
        HttpResponse.json(
          meResponse({
            idleExpiresAt: START + 10 * MINUTE_MS,
            absoluteExpiresAt: START + 12 * HOUR_MS,
          }),
        ),
      ),
    );
    await store.load();
    clock.advance(8 * MINUTE_MS);
    expect(notices).toEqual(['idle_warning']);
  });
});

describe('aviso de vencimiento absoluto (BR-APP-15)', () => {
  it('sale 10 min antes de absolute_expires_at, una vez, y ninguna respuesta lo prolonga', async () => {
    const { clock, notices, store, me } = setup();
    const absolute = START + 40 * MINUTE_MS;
    server.use(me({ absolute }));
    await store.load();
    // Respuestas cada 5 min: la inactividad nunca avisa.
    for (let t = 0; t < 29; t += 5) {
      clock.advance(5 * MINUTE_MS);
      store.transportHooks.onAuthenticatedResponse();
    }
    expect(clock.now()).toBe(START + 30 * MINUTE_MS);
    expect(notices).toEqual(['absolute_warning']);
    expect(store.getSnapshot().status).toBe('expiring_soon');
    // «Seguir trabajando» y más respuestas no lo retiran ni lo repiten.
    await store.continueWorking();
    store.transportHooks.onAuthenticatedResponse();
    expect(store.getSnapshot()).toMatchObject({
      status: 'expiring_soon',
      absolute_deadline_at: absolute,
    });
    clock.advance(ABSOLUTE_WARNING_BEFORE_MS - 1);
    expect(store.getSnapshot().status).toBe('expiring_soon');
    clock.advance(1);
    expect(store.getSnapshot().status).toBe('expired_absolute');
    expect(notices).toEqual(['absolute_warning']);
  });
});

describe('puerta de actividad (D-4)', () => {
  it('se cierra a los 25 min sin interacción y la reabre un clic, una tecla o un desplazamiento', () => {
    const clock = new FakeClock(START);
    const target = new EventTarget();
    const gate = createActivityGate({ clock, target });
    for (const type of ['click', 'keydown', 'scroll']) {
      clock.advance(ACTIVITY_GATE_MS - 1);
      expect(gate.isOpen()).toBe(true);
      clock.advance(1);
      expect(gate.isOpen()).toBe(false);
      target.dispatchEvent(new Event(type));
      expect(gate.isOpen()).toBe(true);
      expect(gate.lastActivityAt()).toBe(clock.now());
    }
    gate.dispose();
    clock.advance(ACTIVITY_GATE_MS);
    target.dispatchEvent(new Event('click'));
    expect(gate.isOpen()).toBe(false);
  });

  it('escucha en window por omisión, en captura', () => {
    const clock = new FakeClock(START);
    const gate = createActivityGate({ clock });
    clock.advance(ACTIVITY_GATE_MS);
    const inner = document.createElement('button');
    inner.addEventListener('click', (event) => {
      event.stopPropagation();
    });
    document.body.append(inner);
    inner.click();
    expect(gate.isOpen()).toBe(true);
    gate.dispose();
    inner.remove();
  });
});

describe('ports.base avisa a la sesión (VIG-292 + VIG-300)', () => {
  it('2xx y errores de negocio renuevan la ventana; unauthenticated bloquea', async () => {
    const { clock, store, me } = setup();
    server.use(me());
    await store.load();
    const listUsers = () =>
      currentTransport()
        .request({ operationId: 'list_users_users_get', kind: 'read' })
        .catch(() => undefined);
    clock.advance(20 * MINUTE_MS);
    server.use(variant('list_users_users_get', () => HttpResponse.json({ items: [] })));
    await listUsers();
    expect(store.getSnapshot()).toMatchObject({
      last_request_at: START + 20 * MINUTE_MS,
      idle_deadline_at: START + 20 * MINUTE_MS + IDLE_TIMEOUT_MS,
    });
    clock.advance(5 * MINUTE_MS);
    server.use(variant('list_users_users_get', () => apiError('not_found', 404)));
    await listUsers();
    expect(store.getSnapshot().last_request_at).toBe(START + 25 * MINUTE_MS);
    clock.advance(MINUTE_MS);
    server.use(variant('list_users_users_get', () => apiError('unauthenticated', 401)));
    await listUsers();
    expect(store.getSnapshot()).toMatchObject({
      status: 'locked_for_reauth',
      last_request_at: START + 25 * MINUTE_MS,
    });
  });

  it.each([
    ['429 rate_limited', () => apiError('rate_limited', 429)],
    ['503 temporarily_unavailable del bulkhead', () => apiError('temporarily_unavailable', 503)],
    ['413 payload_too_large', () => apiError('payload_too_large', 413)],
    ['500 internal_error', () => apiError('internal_error', 500)],
    ['502 de la pasarela sin cuerpo', () => new HttpResponse(null, { status: 502 })],
  ])('%s llega antes de SessionStep: no renueva la ventana', async (_, reply) => {
    const { clock, notices, store, me } = setup();
    server.use(me());
    await store.load();
    clock.advance(20 * MINUTE_MS);
    server.use(variant('list_users_users_get', reply));
    await currentTransport()
      .request({ operationId: 'list_users_users_get', kind: 'write' })
      .catch(() => undefined);
    expect(store.getSnapshot()).toMatchObject({
      last_request_at: START,
      idle_deadline_at: START + IDLE_TIMEOUT_MS,
    });
    // El aviso sale a los 28 min de la última respuesta autenticada, antes del vencimiento real.
    clock.advance(8 * MINUTE_MS);
    expect(notices).toEqual(['idle_warning']);
  });

  it('privacy_notice_required pasa a privacy_notice_pending', async () => {
    const { store } = setup();
    server.use(variant('me_me_get', () => apiError('privacy_notice_required', 403)));
    await expect(store.load()).rejects.toThrow();
    expect(store.getSnapshot()).toMatchObject({
      status: 'privacy_notice_pending',
      return_route: FORM_ROUTE,
    });
  });
});

/** Formulario de invitación de prueba: borrador en memoria y envío por `ports.base`. */
function InviteForm() {
  const [draft, dispatch] = useFormDraft('SCR-01', 'invite');
  const value = draft.values.email;
  return createElement(
    'form',
    {
      onSubmit: (event: SyntheticEvent) => {
        event.preventDefault();
        void currentTransport()
          .request({ operationId: 'invite_users_post', kind: 'write', body: draft.values })
          .catch(() => undefined);
      },
    },
    createElement('label', { htmlFor: 'email' }, 'Correo'),
    createElement('input', {
      id: 'email',
      value: typeof value === 'string' ? value : '',
      onChange: (event: ChangeEvent<HTMLInputElement>) => {
        dispatch({ type: 'set_value', field: 'email', value: event.target.value });
      },
    }),
    createElement('output', { 'aria-label': 'sucio' }, String(draft.dirty)),
    createElement('button', { type: 'submit' }, 'Invitar'),
  );
}

/**
 * El formulario montado por el enrutador en `route`: `useFormDraft` recupera el borrador de la
 * ruta que pinta la pantalla, como en la aplicación.
 */
function renderInviteForm(store: SessionStore, route: string) {
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: [route] },
      createElement(SessionProvider, { store }, createElement(InviteForm)),
    ),
  );
}

describe('unauthenticated a mitad de un formulario (BR-APP-16, PAT-APP-RES-05, NFR-APP-38)', () => {
  async function lockMidForm(options: {
    reauthAs: string;
    mountWhileLocked?: boolean;
    detour?: boolean;
  }) {
    const context = setup();
    const { clock, store, me } = context;
    server.use(me());
    await store.load();
    let posts = 0;
    server.use(
      variant('invite_users_post', () => {
        posts += 1;
        return apiError('unauthenticated', 401);
      }),
    );
    const view = renderInviteForm(store, context.route.current);
    fireEvent.change(screen.getByLabelText('Correo'), { target: { value: 'nueva@ejemplo.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Invitar' }));
    await vi.waitFor(() => {
      expect(store.getSnapshot().status).toBe('locked_for_reauth');
    }, WAIT);
    // El armazón lleva a /login: la pantalla se desmonta con la sesión bloqueada.
    view.unmount();
    context.route.current = LOGIN_ROUTE;
    if (options.mountWhileLocked === true) {
      // Antes de saber quién reautentica, el formulario no recupera nada; al desmontarse en
      // blanco no pisa el borrador guardado.
      context.route.current = FORM_ROUTE;
      const locked = renderInviteForm(store, context.route.current);
      expect(screen.getByLabelText('Correo')).toHaveProperty('value', '');
      locked.unmount();
      context.route.current = LOGIN_ROUTE;
    }
    clock.advance(10 * MINUTE_MS);
    server.use(me({ userId: options.reauthAs }));
    await act(async () => {
      await store.load();
    });
    const back = store.takeReturnRoute();
    if (options.detour === true) {
      // La persona pasa antes por otra pantalla con el mismo formulario: el borrador no es suyo.
      context.route.current = '/users';
      const other = renderInviteForm(store, context.route.current);
      expect(screen.getByLabelText('Correo')).toHaveProperty('value', '');
      other.unmount();
    }
    // Sin ruta guardada, la persona vuelve a abrir el mismo formulario por su cuenta.
    context.route.current = back ?? FORM_ROUTE;
    renderInviteForm(store, context.route.current);
    // Tiempo para los temporizadores y una petición testigo por el mismo transporte y MSW: una
    // escritura reenviada saldría antes que ella.
    clock.advance(10 * MINUTE_MS);
    let probes = 0;
    server.use(
      variant('list_users_users_get', () => {
        probes += 1;
        return HttpResponse.json({ items: [] });
      }),
    );
    await act(async () => {
      await currentTransport().request({ operationId: 'list_users_users_get', kind: 'read' });
    });
    expect(probes).toBe(1);
    return { back, posts: () => posts };
  }

  it('la misma persona vuelve a la ruta con el borrador; la escritura no se reenvía', async () => {
    const { back, posts } = await lockMidForm({ reauthAs: USER_A });
    expect(back).toBe(FORM_ROUTE);
    expect(screen.getByLabelText('Correo')).toHaveProperty('value', 'nueva@ejemplo.test');
    expect(screen.getByLabelText('sucio').textContent).toBe('true');
    expect(posts()).toBe(1);
  });

  it('con la sesión bloqueada el formulario no recupera el borrador ni lo pisa', async () => {
    const { back, posts } = await lockMidForm({ reauthAs: USER_A, mountWhileLocked: true });
    expect(back).toBe(FORM_ROUTE);
    expect(screen.getByLabelText('Correo')).toHaveProperty('value', 'nueva@ejemplo.test');
    expect(posts()).toBe(1);
  });

  it('el borrador espera en su ruta aunque la persona pase antes por otra', async () => {
    const { posts } = await lockMidForm({ reauthAs: USER_A, detour: true });
    expect(screen.getByLabelText('Correo')).toHaveProperty('value', 'nueva@ejemplo.test');
    expect(posts()).toBe(1);
  });

  it('otra persona no recupera ni la ruta ni el borrador', async () => {
    const { back, posts } = await lockMidForm({ reauthAs: USER_B });
    expect(back).toBeNull();
    expect(screen.getByLabelText('Correo')).toHaveProperty('value', '');
    expect(posts()).toBe(1);
  });
});

describe('logout() lo borra todo (BR-APP-17, BR-APP-19)', () => {
  async function loggedIn(logout: Parameters<typeof variant>[1]) {
    const context = setup();
    let posts = 0;
    server.use(
      context.me(),
      variant('logout_auth_logout_post', (info) => {
        posts += 1;
        return logout(info);
      }),
    );
    await context.store.load();
    context.queries.client.setQueryData(
      screenKey({
        screen: 'scr08',
        operation: 'GET /loop/findings',
        scope: 'all',
        concession: null,
      }),
      { items: [{ id: 'x' }] },
    );
    localStorage.setItem('vigia.ui_preferences', JSON.stringify({ selected_plant_id: 'p' }));
    sessionStorage.setItem('otra', '1');
    // Un borrador conservado de un bloqueo anterior tampoco sobrevive.
    context.store.transportHooks.onSessionEvent('unauthenticated');
    context.store.keepDraft({
      screenId: 'SCR-01',
      formId: 'invite',
      stepIndex: 0,
      values: { email: 'a@ejemplo.test' },
      dirty: true,
      serverProgressRef: null,
      preservedOnLock: false,
    });
    return { ...context, posts: () => posts };
  }

  it.each([
    ['204', () => new HttpResponse(null, { status: 204 })],
    ['500', () => apiError('internal_error', 500)],
    ['red caída', () => HttpResponse.error()],
  ] as const)(
    'con respuesta %s: POST /auth/logout y caché, almacenamiento y estado vacíos',
    async (_, logout) => {
      const { store, queries, clearPreferences, navigate, posts, route, me } =
        await loggedIn(logout);
      await store.logout();
      expect(posts()).toBe(1);
      expect(queries.client.getQueryCache().getAll()).toEqual([]);
      expect(localStorage.length).toBe(0);
      expect(sessionStorage.length).toBe(0);
      expect(clearPreferences).toHaveBeenCalledTimes(1);
      expect(store.getSnapshot()).toBe(initialSession);
      expect(navigate).toHaveBeenCalledWith(LOGIN_ROUTE);
      // Ni el borrador del bloqueo ni la ruta vuelven tras otro inicio de sesión.
      server.use(me());
      await store.load();
      expect(store.takeReturnRoute()).toBeNull();
      route.current = FORM_ROUTE;
      expect(store.takeDraft('SCR-01', 'invite')).toBeNull();
    },
  );
});
