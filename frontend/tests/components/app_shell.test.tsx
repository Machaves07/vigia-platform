/**
 * UI-01 `AppShell`, UI-02 `Header`, UI-05 `SessionNotice`, UI-06 `ConnectionNotice`, UI-07
 * `NavMenu` y el arranque (paso 3 de PAT-APP-REN-01) con MSW y el reloj simulado: `GET /me` y
 * `GET /hierarchy` en paralelo, fallo cerrado si `GET /me` falla, bloqueo `locked_for_reauth` con
 * reautenticación que restaura ruta y borrador, menú en orden fijo, avisos permanentes, cierre de
 * sesión que lo vacía todo y `axe` sobre el armazón completo. Datos sintéticos.
 */
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { Link } from 'react-router';
import { axe } from 'vitest-axe';

import { connectionMessageKey } from '@/app/ConnectionNotice';
import { buildNavigationMap } from '@/app/navigation';
import { navItems } from '@/app/NavMenu';
import { SCREENS as APP_SCREENS } from '@/app/screens';
import { lockMessageKey } from '@/app/SessionNotice';
import { PREFERENCES_KEY } from '@/state/scope';
import { useFormDraft, useSession } from '@/state/session';
import { text } from '@/texts';

import { apiError } from '../msw/handlers';
import { server } from '../setup/msw';
import {
  advance,
  mountShell,
  ORIGIN,
  PLANT_ID,
  serveSession,
  shellHierarchy,
  shellMe,
  START,
  syntheticScreen,
  WAIT,
} from '../support/appShell';

// Aquí se aíslan los avisos de la sesión y la conexión: la campana ya es una consulta viva.
// SCR-15 recorre la campana real, también dentro de AppShell, en components/scr15/bell.test.tsx.
vi.mock('@/pages/scr15/Bell', () => ({
  Bell: () => <Link to="/notifications">{text('navigation.bell')}</Link>,
}));

const TEST = { timeout: 60_000 };
const DRAFT = 'Observación sintética del formulario';
const EMAIL = 'persona@ejemplo.test';
const PASSWORD = 'clave-sintetica-larga';

/** Pantalla con un formulario cuyo borrador vive en `useFormDraft` y un botón que pide `GET /me`. */
function FormPage(): ReactNode {
  const session = useSession();
  const [draft, dispatch] = useFormDraft('SCR-08', 'note');
  const value = draft.values.note;
  return (
    <main>
      <h1>Formulario sintético</h1>
      <label htmlFor="note">Nota</label>
      <input
        id="note"
        value={typeof value === 'string' ? value : ''}
        onChange={(event) => {
          dispatch({ type: 'set_value', field: 'note', value: event.currentTarget.value });
        }}
      />
      <button
        type="button"
        onClick={() => {
          session.load().catch(() => undefined);
        }}
      >
        Consultar
      </button>
    </main>
  );
}

function PlainPage({ title }: { readonly title: string }): ReactNode {
  return (
    <main>
      <h1>{title}</h1>
    </main>
  );
}

const SCREENS = [
  syntheticScreen({
    id: 'SCR-07',
    title: 'navigation.scr07',
    path: '/fleet',
    permission: 'fleet.read',
    page: () => <PlainPage title="Flota sintética" />,
  }),
  syntheticScreen({
    id: 'SCR-08',
    title: 'navigation.scr08',
    path: '/findings',
    permission: 'findings.read',
    page: FormPage,
    home: true,
  }),
  // «Cola de revisión» va antes que «Flota» por orden alfabético, no en la tabla.
  syntheticScreen({
    id: 'SCR-10',
    title: 'navigation.scr10',
    path: '/review-queue',
    permission: 'review_queue.resolve',
    page: () => <PlainPage title="Cola sintética" />,
  }),
  syntheticScreen({
    id: 'SCR-13',
    title: 'navigation.scr13',
    path: '/metrics',
    permission: 'metrics.read',
    page: () => <PlainPage title="Métricas sintéticas" />,
  }),
  syntheticScreen({
    id: 'SCR-15',
    title: 'navigation.scr15',
    path: '/notifications',
    permission: 'notifications.read',
    page: () => <PlainPage title="Notificaciones sintéticas" />,
  }),
];

/** Permisos sin `metrics.read`: SCR-13 no es visible. */
const PERMISSIONS = ['fleet.read', 'findings.read', 'review_queue.resolve', 'notifications.read'];

function navigation(): HTMLElement {
  return screen.getByRole('navigation', { name: text('app_shell.nav.label') });
}

function navNames(): string[] {
  return within(navigation())
    .getAllByRole('link')
    .map((link) => link.textContent);
}

type Violations = Awaited<ReturnType<typeof axe>>['violations'];

async function seriousViolations(container: HTMLElement): Promise<string[]> {
  const results = await axe(container);
  return results.violations
    .filter((violation: Violations[number]) =>
      ['serious', 'critical'].includes(violation.impact ?? ''),
    )
    .map((violation: Violations[number]) => violation.id);
}

describe('arranque: GET /me y GET /hierarchy (PAT-APP-REN-01, PAT-APP-RES-07)', TEST, () => {
  it('con GET /me en temporarily_unavailable no se pinta navegación ni pantalla', async () => {
    let meCalls = 0;
    server.use(
      http.get(`${ORIGIN}/me`, () => {
        meCalls += 1;
        return apiError('temporarily_unavailable', 503);
      }),
      http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(shellHierarchy())),
    );
    const fixture = mountShell('/findings', SCREENS);
    // Los reintentos de lectura (2, 4 y 8 s) corren con el reloj simulado.
    const failed = await waitFor(() => {
      advance(fixture.clock, 1_000);
      const alert = screen.getByRole('alert');
      expect(alert.dataset.textKey).toBe('app_shell.boot.failed');
      return alert;
    }, WAIT);
    expect(meCalls).toBeGreaterThanOrEqual(1);
    expect(
      within(failed).getByRole('button', { name: text('app_shell.boot.retry') }),
    ).toBeDefined();
    // Ni navegación, ni cabecera, ni pantalla: solo el nombre de la aplicación y el aviso.
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getAllByRole('heading').map((heading) => heading.textContent)).toEqual([
      text('common.app_name'),
    ]);
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(fixture.services.session.getSnapshot().principal).toBeNull();
  });

  it('«repetir» vuelve a pedir GET /me y, si responde, monta el armazón', async () => {
    let available = false;
    server.use(
      http.get(`${ORIGIN}/me`, () =>
        available
          ? HttpResponse.json(shellMe(PERMISSIONS))
          : apiError('internal_error', 500, { operationId: 'me_me_get' }),
      ),
      http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(shellHierarchy())),
    );
    mountShell('/findings', SCREENS);
    const retry = await screen.findByRole('button', { name: text('app_shell.boot.retry') }, WAIT);
    expect(screen.queryByRole('navigation')).toBeNull();
    available = true;
    await userEvent.setup().click(retry);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    expect(navigation()).toBeDefined();
  });

  it('sin sesión (unauthenticated) no hay armazón: solo la ruta pública', async () => {
    server.use(
      http.get(`${ORIGIN}/me`, () => apiError('unauthenticated', 401)),
      http.get(`${ORIGIN}/hierarchy`, () => apiError('unauthenticated', 401)),
    );
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();
    expect(fixture.route()).toBe('/findings');
  });

  it('GET /me y GET /hierarchy salen en paralelo y la jerarquía llena el selector', async () => {
    const order: string[] = [];
    let releaseMe: () => void = () => undefined;
    const meGate = new Promise<void>((resolve) => {
      releaseMe = resolve;
    });
    server.use(
      http.get(`${ORIGIN}/me`, async () => {
        order.push('me:start');
        await meGate;
        order.push('me:end');
        return HttpResponse.json(shellMe(PERMISSIONS));
      }),
      http.get(`${ORIGIN}/hierarchy`, () => {
        order.push('hierarchy');
        return HttpResponse.json(shellHierarchy());
      }),
    );
    mountShell('/findings', SCREENS);
    // La jerarquía se pide mientras GET /me sigue sin responder (el orden de salida da igual).
    await waitFor(() => {
      expect([...order].sort()).toEqual(['hierarchy', 'me:start']);
    }, WAIT);
    expect(screen.queryByRole('navigation')).toBeNull();
    releaseMe();
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    const selector = screen.getByLabelText(text('components_scope.selector.label'));
    await waitFor(() => {
      expect(
        within(selector)
          .getAllByRole('option')
          .map((option) => option.getAttribute('value')),
      ).toEqual(['all', PLANT_ID]);
    }, WAIT);
    expect(order.at(-1)).toBe('me:end');
  });
});

describe('UI-07 NavMenu: solo pantallas visibles, en orden fijo (BR-APP-01, 03)', TEST, () => {
  it('pinta las entradas visibles de NavigationMap en el orden de la tabla', async () => {
    serveSession(shellMe(PERMISSIONS));
    mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    expect(navNames()).toEqual([
      text('navigation.scr07'),
      text('navigation.scr08'),
      text('navigation.scr10'),
      text('navigation.scr15'),
    ]);
    const current = within(navigation()).getByRole('link', { name: text('navigation.scr08') });
    expect(current.getAttribute('aria-current')).toBe('page');
  });

  it('con la tabla real: orden fijo, sin pantallas sin permiso ni rutas públicas o de detalle', () => {
    const me = shellMe([
      'users.manage',
      'notifications.read',
      'metrics.read',
      'fleet.read',
      'findings.read',
      'coverage.read',
    ]);
    const principal = {
      effective_permissions: me.effective_permissions,
      assignments: me.assignments,
    };
    const items = navItems(buildNavigationMap(principal, APP_SCREENS), APP_SCREENS);
    // SCR-01 lleva a la cuenta, nunca a su grupo público de inicio de sesión; SCR-03 solo tiene
    // con `coverage.read` una ruta de detalle y no tiene entrada.
    expect(items.map((item) => [item.screen_id, item.path])).toEqual([
      ['SCR-01', '/account'],
      ['SCR-07', '/fleet'],
      ['SCR-08', '/home'],
      ['SCR-10', '/review-queue'],
      ['SCR-13', '/metrics'],
      ['SCR-15', '/notifications'],
    ]);
    // Sin ninguna clave de SCR-01, su cuenta (abierta a cualquier sesión) no da entrada al menú.
    const only = shellMe(['notifications.read']);
    const narrow = navItems(buildNavigationMap(only, APP_SCREENS), APP_SCREENS);
    expect(narrow.map((item) => [item.screen_id, item.path])).toEqual([
      ['SCR-15', '/notifications'],
    ]);
  });
});

describe('UI-05 SessionNotice (BR-APP-14 a 16)', TEST, () => {
  it('locked_for_reauth bloquea con la reautenticación y al volver restaura ruta y borrador', async () => {
    let unauthenticated = false;
    let logins = 0;
    server.use(
      http.get(`${ORIGIN}/me`, () =>
        unauthenticated
          ? apiError('unauthenticated', 401)
          : HttpResponse.json(shellMe(PERMISSIONS), {
              headers: { 'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION },
            }),
      ),
      http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(shellHierarchy())),
      http.post(`${ORIGIN}/auth/login`, () => {
        logins += 1;
        unauthenticated = false;
        return HttpResponse.json({ status: 'authenticated' });
      }),
    );
    const fixture = mountShell('/findings', SCREENS);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Nota', {}, WAIT), DRAFT);

    unauthenticated = true;
    await user.click(screen.getByRole('button', { name: 'Consultar' }));
    const lock = await screen.findByRole('alert', {}, WAIT);
    expect(lock.dataset.textKey).toBe('errors.reauth');
    expect(fixture.services.session.getSnapshot().status).toBe('locked_for_reauth');
    // La pantalla queda bloqueada: ni el formulario, ni la cabecera, ni el menú.
    expect(screen.queryByLabelText('Nota')).toBeNull();
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();
    expect(fixture.route()).toBe('/findings');

    await user.type(screen.getByLabelText(text('scr01.login.email')), EMAIL);
    await user.type(screen.getByLabelText(text('scr01.login.password')), PASSWORD);
    await user.click(screen.getByRole('button', { name: text('scr01.login.submit') }));

    const restored = await screen.findByLabelText('Nota', {}, WAIT);
    expect((restored as HTMLInputElement).value).toBe(DRAFT);
    expect(fixture.route()).toBe('/findings');
    expect(fixture.services.session.getSnapshot().status).toBe('active');
    expect(navigation()).toBeDefined();
    expect(logins).toBe(1);
  });

  it('aviso de los 28 min con «seguir trabajando» (GET /me) y aviso absoluto', async () => {
    let renewed = false;
    server.use(
      http.get(`${ORIGIN}/me`, () => {
        const me = shellMe(PERMISSIONS);
        return HttpResponse.json(
          renewed ? { ...me, idle_expires_at: new Date(START + 3_600_000).toISOString() } : me,
        );
      }),
      http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(shellHierarchy())),
    );
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    expect(screen.queryByRole('region', { name: text('app_shell.session.label') })).toBeNull();

    advance(fixture.clock, 28 * 60_000);
    const idle = await screen.findByRole('alert', {}, WAIT);
    expect(idle.dataset.textKey).toBe('app_shell.session.idle');
    renewed = true;
    await userEvent
      .setup()
      .click(within(idle).getByRole('button', { name: text('app_shell.session.keep_working') }));
    await waitFor(() => {
      expect(screen.queryByRole('alert')).toBeNull();
    }, WAIT);
    expect(fixture.services.session.getSnapshot().status).toBe('active');
  });

  it('el aviso absoluto sale 10 min antes y ninguna acción lo retira', async () => {
    serveSession({
      ...shellMe(PERMISSIONS),
      idle_expires_at: new Date(START + 43_200_000).toISOString(),
      absolute_expires_at: new Date(START + 15 * 60_000).toISOString(),
    });
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    advance(fixture.clock, 5 * 60_000);
    const absolute = await screen.findByRole('alert', {}, WAIT);
    expect(absolute.dataset.textKey).toBe('app_shell.session.absolute');
    expect(within(absolute).queryByRole('button')).toBeNull();
  });
});

describe('UI-06 ConnectionNotice (BR-APP-74)', TEST, () => {
  it('degraded y offline con la hora del último dato y las escrituras deshabilitadas', async () => {
    serveSession(shellMe(PERMISSIONS));
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    const label = { name: text('app_shell.connection.label') };
    expect(screen.queryByRole('region', label)).toBeNull();

    const failure = { ok: false, error: { code: 'temporarily_unavailable', status: 503 } } as const;
    act(() => {
      fixture.services.connection.register('platform', failure);
    });
    let notice = await screen.findByRole('region', label, WAIT);
    expect(notice.dataset.state).toBe('degraded');
    expect(within(notice).getByRole('status').dataset.textKey).toBe(
      'app_shell.connection.degraded',
    );
    expect(
      notice.querySelector('[data-text-key="app_shell.connection.writes_degraded"]'),
    ).not.toBeNull();
    // Hubo respuestas correctas (GET /me): se muestra su hora, no «ningún dato».
    expect(notice.querySelector('[data-part="last_data"] time')).not.toBeNull();

    act(() => {
      for (let i = 0; i < 3; i += 1) fixture.services.connection.register('platform', failure);
    });
    notice = await screen.findByRole('region', label, WAIT);
    expect(notice.dataset.state).toBe('offline');
    expect(within(notice).getByRole('status').dataset.textKey).toBe('app_shell.connection.offline');
    expect(
      notice.querySelector('[data-text-key="app_shell.connection.writes_disabled"]'),
    ).not.toBeNull();
  });
});

describe('UI-02 Header: cuenta y cierre de sesión (BR-APP-05, 17)', TEST, () => {
  it('el menú de cuenta ofrece sesiones, contraseña, segundo factor y cerrar sesión', async () => {
    serveSession(shellMe(PERMISSIONS));
    mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    const user = userEvent.setup();
    const toggle = screen.getByRole('button', { name: text('app_shell.account.menu') });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const menu = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
    if (menu === null) throw new Error('El botón de cuenta no controla ningún menú.');
    const entries = within(menu);
    expect(entries.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      '/account#sessions',
      '/account#password',
      '/account#second-factor',
    ]);
    expect(entries.getByRole('button', { name: text('app_shell.account.logout') })).toBeDefined();
    await user.keyboard('{Escape}');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
  });

  it('cerrar sesión vacía preferencias, selección de planta y concesión, y vuelve a /login', async () => {
    let logouts = 0;
    serveSession(shellMe(PERMISSIONS, { kind: 'provider' }));
    server.use(
      http.post(`${ORIGIN}/auth/logout`, () => {
        logouts += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    const user = userEvent.setup();
    const selector = screen.getByLabelText(text('components_scope.selector.label'));
    await waitFor(() => {
      expect((selector as HTMLSelectElement).disabled).toBe(false);
    }, WAIT);
    await user.selectOptions(selector, PLANT_ID);
    expect(localStorage.getItem(PREFERENCES_KEY)).not.toBeNull();
    act(() => {
      fixture.services.concession.activate({
        concession_id: '0190a1b2-0000-7000-8000-0000000000d1',
        client_organization_id: '0190a1b2-0000-7000-8000-0000000000f2',
        scope_level: 'plant',
        scope_id: PLANT_ID,
        expires_at: new Date(START + 3_600_000).toISOString(),
        status: 'active',
      });
    });
    expect(fixture.services.concession.header()).not.toBeNull();

    await user.click(screen.getByRole('button', { name: text('app_shell.account.menu') }));
    await user.click(screen.getByRole('button', { name: text('app_shell.account.logout') }));
    await waitFor(() => {
      expect(fixture.route()).toBe('/login');
    }, WAIT);
    expect(logouts).toBe(1);
    expect(localStorage.length).toBe(0);
    expect(fixture.services.scope.getSnapshot()).toEqual({
      plantId: 'all',
      availablePlants: null,
      zones: {},
    });
    expect(fixture.services.concession.header()).toBeNull();
    expect(fixture.services.session.getSnapshot().status).toBe('anonymous');
    expect(screen.queryByRole('navigation')).toBeNull();
  });
});

describe('enumeraciones fuera de lista: «desconocido» (NFR-APP-30)', TEST, () => {
  it('estados de conexión y de bloqueo desconocidos usan la etiqueta «desconocido»', () => {
    expect(connectionMessageKey('degraded')).toBe('app_shell.connection.degraded');
    expect(connectionMessageKey('offline')).toBe('app_shell.connection.offline');
    expect(connectionMessageKey('online_plus')).toBe('common.unknown');
    expect(connectionMessageKey('constructor')).toBe('common.unknown');
    expect(lockMessageKey('locked_for_reauth')).toBe('errors.reauth');
    expect(lockMessageKey('expired_idle')).toBe('app_shell.session.expired_idle');
    expect(lockMessageKey('expired_absolute')).toBe('app_shell.session.expired_absolute');
    expect(lockMessageKey('locked_forever')).toBe('common.unknown');
    expect(lockMessageKey('toString')).toBe('common.unknown');
  });

  it('un alcance de concesión fuera de lista se muestra «desconocido» en el aviso', async () => {
    serveSession(shellMe(PERMISSIONS, { kind: 'provider' }));
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    act(() => {
      fixture.services.concession.activate({
        concession_id: '0190a1b2-0000-7000-8000-0000000000d1',
        client_organization_id: '0190a1b2-0000-7000-8000-0000000000f2',
        scope_level: 'region' as 'plant',
        scope_id: PLANT_ID,
        expires_at: new Date(START + 3_600_000).toISOString(),
        status: 'active',
      });
    });
    const banner = await screen.findByRole(
      'region',
      { name: text('components_concession.banner.label') },
      WAIT,
    );
    expect(banner.querySelector('[data-part="scope"]')?.textContent).toBe(text('common.unknown'));
  });
});

describe('axe sobre el armazón completo (NFR-APP-50)', TEST, () => {
  it('sin violaciones serias con todos los avisos y el menú de cuenta abierto', async () => {
    serveSession(shellMe(PERMISSIONS, { kind: 'provider' }));
    const fixture = mountShell('/findings', SCREENS);
    await screen.findByRole('heading', { name: 'Formulario sintético' }, WAIT);
    advance(fixture.clock, 28 * 60_000);
    await screen.findByRole('button', { name: text('app_shell.session.keep_working') }, WAIT);
    // Después del aviso de los 28 min: un fallo cuenta 30 s en la ventana del circuito.
    act(() => {
      fixture.services.connection.register('platform', {
        ok: false,
        error: { code: 'temporarily_unavailable', status: 503 },
      });
      fixture.services.version.markOutdated();
      fixture.services.concession.activate({
        concession_id: '0190a1b2-0000-7000-8000-0000000000d1',
        client_organization_id: '0190a1b2-0000-7000-8000-0000000000f2',
        scope_level: 'plant',
        scope_id: PLANT_ID,
        expires_at: new Date(START + 43_200_000).toISOString(),
        status: 'active',
      });
    });
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: text('app_shell.account.menu') }));
    for (const name of [
      'app_shell.session.label',
      'app_shell.connection.label',
      'app_shell.version.label',
      'components_concession.banner.label',
    ]) {
      expect(screen.getByRole('region', { name: text(name) })).toBeDefined();
    }
    expect(await seriousViolations(fixture.container)).toEqual([]);
  });
});
