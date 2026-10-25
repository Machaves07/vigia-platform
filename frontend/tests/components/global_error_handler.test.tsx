/**
 * UI-09 `GlobalErrorHandler` (LC-APP-04, BR-APP-76, NFR-APP-18 y 30): una excepción inyectada en
 * una pantalla (al pintar, en la ventana o como promesa rechazada sin capturar) pinta la pantalla
 * genérica, saca sus consultas del `QueryClient` y no escribe nada en la consola con la
 * construcción en modo producción (`DEV` falso). Datos sintéticos.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import {
  AppErrorBoundary,
  failureCorrelation,
  rootErrorOptions,
  screensAt,
} from '@/app/GlobalErrorHandler';
import { behaviourFor } from '@/ports/errors/map';
import { PortError } from '@/ports/shared';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';

import {
  mountShell,
  serveSession,
  shellMe,
  syntheticScreen,
  WAIT,
  type ShellFixture,
} from '../support/appShell';

// Se aísla el manejador de errores de la consulta viva de la campana, como en app_shell.test.tsx.
// La campana real dentro de AppShell se prueba en components/scr15/bell.test.tsx.
vi.mock('@/pages/scr15/Bell', () => ({
  Bell: () => <Link to="/notifications">{text('navigation.bell')}</Link>,
}));

const CORRELATION = '0190a1b2-0000-7000-8000-0000000000c9';
/** Texto que solo está en el error: nunca debe llegar a la pantalla. */
const INTERNAL = 'detalle interno sintético de la excepción';

/** Lo que la pantalla sintética lanza al pulsar «Romper». */
let thrown: unknown = new Error(INTERNAL);

function ProbePage(): ReactNode {
  const [broken, setBroken] = useState(false);
  const query = useScreenQuery({
    key: screenKey({ screen: 'scr08', operation: 'GET /hierarchy', scope: 's', concession: null }),
    policy: policyFor('GET /hierarchy'),
    read: () => Promise.resolve({ plants: 1 }),
  });
  if (broken) throw thrown;
  return (
    <main>
      <h1>Pantalla sintética</h1>
      <p>{query.status === 'success' ? 'Datos cargados' : 'Cargando'}</p>
      <button
        type="button"
        onClick={() => {
          setBroken(true);
        }}
      >
        Romper
      </button>
    </main>
  );
}

function OtherPage(): ReactNode {
  return (
    <main>
      <h1>Otra pantalla sintética</h1>
    </main>
  );
}

const SCREENS = [
  syntheticScreen({
    id: 'SCR-08',
    title: 'navigation.scr08',
    path: '/findings',
    permission: 'findings.read',
    page: ProbePage,
    home: true,
  }),
  syntheticScreen({
    id: 'SCR-15',
    title: 'navigation.scr15',
    path: '/notifications',
    permission: 'notifications.read',
    page: OtherPage,
  }),
];

const METHODS = ['error', 'warn', 'log', 'info', 'debug'] as const;
let spies: readonly MockInstance[] = [];

beforeEach(() => {
  // La construcción de producción: `DEV` falso (el registro de desarrollo calla).
  vi.stubEnv('DEV', false);
  vi.stubEnv('PROD', true);
  spies = METHODS.map((method) => vi.spyOn(console, method).mockImplementation(() => undefined));
  thrown = new Error(INTERNAL);
  serveSession(shellMe(['findings.read', 'notifications.read']));
});

afterEach(() => {
  vi.unstubAllEnvs();
});

function consoleCalls(): Record<string, number> {
  return Object.fromEntries(
    METHODS.map((method, index) => [method, spies[index]?.mock.calls.length ?? -1]),
  );
}

const SILENT = { error: 0, warn: 0, log: 0, info: 0, debug: 0 };

function queriesOf(fixture: ShellFixture, screenName: string): number {
  return fixture.services.cache.client.getQueryCache().findAll({ queryKey: [screenName] }).length;
}

/** Monta en `/findings`, espera los datos y deja otra pantalla con una consulta en la caché. */
async function mountLoaded(): Promise<ShellFixture> {
  const fixture = mountShell('/findings', SCREENS);
  await screen.findByText('Datos cargados', {}, WAIT);
  fixture.services.cache.client.setQueryData(['scr15', 'GET /notifications'], { items: [] });
  expect(queriesOf(fixture, 'scr08')).toBe(1);
  return fixture;
}

async function expectGenericScreen(): Promise<HTMLElement> {
  const alert = await screen.findByRole('alert', {}, WAIT);
  expect(alert.dataset.textKey).toBe('app_shell.error.title');
  expect(
    screen.getByRole('heading', { level: 1, name: text('app_shell.error.title') }),
  ).toBeDefined();
  expect(screen.queryByRole('heading', { name: 'Pantalla sintética' })).toBeNull();
  expect(screen.queryByText('Datos cargados')).toBeNull();
  return alert;
}

describe('excepción al pintar una pantalla (BR-APP-76)', { timeout: 30_000 }, () => {
  it('pantalla genérica, consultas de la pantalla descartadas y nada en la consola', async () => {
    const fixture = await mountLoaded();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Romper' }));
    const alert = await expectGenericScreen();
    await waitFor(() => {
      expect(queriesOf(fixture, 'scr08')).toBe(0);
    }, WAIT);
    // Solo la pantalla afectada: la consulta de otra pantalla sigue en la caché.
    expect(queriesOf(fixture, 'scr15')).toBe(1);
    // Ningún detalle interno: ni el mensaje de la excepción ni un código de correlación inventado.
    expect(fixture.container.innerHTML).not.toContain(INTERNAL);
    expect(alert.querySelector('[data-part="correlation_id"]')).toBeNull();
    // El armazón sigue: la navegación permite salir de la pantalla genérica.
    expect(screen.getByRole('navigation', { name: text('app_shell.nav.label') })).toBeDefined();
    expect(consoleCalls()).toEqual(SILENT);
  });

  it('con una respuesta de la API, el correlation_id se muestra copiable y nada más', async () => {
    thrown = new PortError({
      kind: 'http',
      error: {
        code: 'internal_error',
        status: 500,
        message_es: INTERNAL,
        body: { code: 'internal_error', message_es: INTERNAL, correlation_id: CORRELATION },
      },
      behaviour: behaviourFor({ code: 'internal_error', status: 500 }),
    });
    const fixture = await mountLoaded();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Romper' }));
    const alert = await expectGenericScreen();
    expect(alert.querySelector('[data-part="correlation_id"]')?.textContent).toBe(CORRELATION);
    expect(screen.getByRole('button', { name: text('app_shell.error.copy') })).toBeDefined();
    expect(fixture.container.innerHTML).not.toContain(INTERNAL);
    expect(consoleCalls()).toEqual(SILENT);
  });

  it('navegar a otra pantalla sale de la pantalla genérica', async () => {
    const fixture = await mountLoaded();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Romper' }));
    await expectGenericScreen();
    const nav = screen.getByRole('navigation', { name: text('app_shell.nav.label') });
    await user.click(within(nav).getByRole('link', { name: text('navigation.scr15') }));
    await screen.findByRole('heading', { name: 'Otra pantalla sintética' }, WAIT);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(fixture.route()).toBe('/notifications');
    expect(consoleCalls()).toEqual(SILENT);
  });
});

describe('fallos de la ventana (window.onerror y unhandledrejection)', { timeout: 30_000 }, () => {
  it('un error de la ventana pinta la pantalla genérica, descarta y cancela el informe', async () => {
    const fixture = await mountLoaded();
    const event = new ErrorEvent('error', {
      error: new Error(INTERNAL),
      message: INTERNAL,
      cancelable: true,
    });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    await expectGenericScreen();
    await waitFor(() => {
      expect(queriesOf(fixture, 'scr08')).toBe(0);
    }, WAIT);
    expect(queriesOf(fixture, 'scr15')).toBe(1);
    expect(fixture.container.innerHTML).not.toContain(INTERNAL);
    expect(consoleCalls()).toEqual(SILENT);
  });

  it('una promesa rechazada sin capturar pinta la pantalla genérica y cancela el informe', async () => {
    const fixture = await mountLoaded();
    const event = new Event('unhandledrejection', { cancelable: true });
    Object.defineProperty(event, 'reason', { value: new Error(INTERNAL) });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    await expectGenericScreen();
    await waitFor(() => {
      expect(queriesOf(fixture, 'scr08')).toBe(0);
    }, WAIT);
    expect(consoleCalls()).toEqual(SILENT);
  });

  it('una cancelación no es un fallo: la pantalla sigue', async () => {
    const fixture = await mountLoaded();
    const event = new Event('unhandledrejection', { cancelable: true });
    Object.defineProperty(event, 'reason', {
      value: new DOMException('cancelada', 'AbortError'),
    });
    window.dispatchEvent(event);
    expect(fixture.services.errors.getSnapshot()).toBeNull();
    expect(screen.getByRole('heading', { name: 'Pantalla sintética' })).toBeDefined();
    expect(queriesOf(fixture, 'scr08')).toBe(1);
  });
});

describe('piezas del manejador', { timeout: 30_000 }, () => {
  it('el último límite pinta la pantalla genérica sin enrutador y sin consola', () => {
    function Thrower(): ReactNode {
      throw new Error(INTERNAL);
    }
    const view = render(
      <AppErrorBoundary>
        <Thrower />
      </AppErrorBoundary>,
      { onCaughtError: rootErrorOptions.onCaughtError },
    );
    expect(screen.getByRole('alert').dataset.textKey).toBe('app_shell.error.title');
    expect(
      screen.getByRole('link', { name: text('app_shell.error.home') }).getAttribute('href'),
    ).toBe('/');
    expect(view.container.innerHTML).not.toContain(INTERNAL);
    expect(consoleCalls()).toEqual(SILENT);
  });

  it('la pantalla de una ruta es la de su grupo, y la de inicio también «home»', () => {
    expect(screensAt('/findings', SCREENS)).toEqual(['scr08', 'home']);
    expect(screensAt('/notifications', SCREENS)).toEqual(['scr15']);
    expect(screensAt('/otra', SCREENS)).toEqual([]);
  });

  it('solo un PortError con respuesta HTTP aporta correlation_id, y solo si es un UUID', () => {
    const http = (correlation: unknown) =>
      new PortError({
        kind: 'http',
        error: { code: 'internal_error', status: 500, body: { correlation_id: correlation } },
        behaviour: behaviourFor({ code: 'internal_error', status: 500 }),
      });
    expect(failureCorrelation(http(CORRELATION))).toBe(CORRELATION);
    expect(failureCorrelation(http('no-es-un-uuid'))).toBeNull();
    expect(failureCorrelation(new PortError({ kind: 'offline' }))).toBeNull();
    expect(failureCorrelation(new Error(CORRELATION))).toBeNull();
  });
});
