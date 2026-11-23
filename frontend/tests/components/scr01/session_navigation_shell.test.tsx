/**
 * Navegación de la sesión sobre el armazón real (`App`, `AppRouter`, `SessionGate`, `ShellLayout`),
 * con MSW y datos sintéticos (revisión del PR 335, ronda 2):
 *
 * - Aceptar un aviso de tratamiento a mitad de sesión vuelve a la ruta en que ya se estaba. Esa
 *   navegación no puede quedar pendiente: si quedara, la siguiente reconstrucción del enrutador
 *   (un `GET /me` con otros permisos) devolvería a la persona a la ruta vieja.
 * - El puente del enrutador pasa `replace` a la navegación real: la entrada no deja la ruta de
 *   acceso en el historial.
 */
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { Link } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { SCREENS as APP_SCREENS } from '@/app/screens';
import { text } from '@/texts';

import { apiError } from '../../msw/handlers';
import { server } from '../../setup/msw';
import {
  mountShell,
  ORIGIN,
  shellHierarchy,
  shellMe,
  syntheticScreen,
  WAIT,
} from '../../support/appShell';

// La campana real es una consulta viva; aquí solo importa la navegación.
vi.mock('@/pages/scr15/Bell', () => ({
  Bell: () => <Link to="/notifications">{text('navigation.bell')}</Link>,
}));

const PERMISSIONS = ['findings.read', 'notifications.read'];
const SCREENS = [
  syntheticScreen({
    id: 'SCR-08',
    title: 'navigation.scr08',
    path: '/findings',
    permission: 'findings.read',
    page: () => (
      <main>
        <h1>Inicio sintético</h1>
      </main>
    ),
    home: true,
  }),
  syntheticScreen({
    id: 'SCR-10',
    title: 'navigation.scr10',
    path: '/other',
    permission: 'notifications.read',
    page: () => (
      <main>
        <h1>Otra sintética</h1>
      </main>
    ),
  }),
  ...APP_SCREENS.filter((descriptor) => descriptor.screen_id === 'SCR-01'),
];

interface State {
  noticeAccepted: boolean;
  permissions: string[];
}

function serve(state: State) {
  server.use(
    http.get(`${ORIGIN}/me`, () =>
      state.noticeAccepted
        ? HttpResponse.json(
            { ...shellMe(state.permissions), privacy_notice_version: 'v0-pendiente' },
            { headers: { 'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION } },
          )
        : apiError('privacy_notice_required', 403),
    ),
    http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(shellHierarchy())),
    http.post(`${ORIGIN}/privacy-notice/accept`, () => {
      state.noticeAccepted = true;
      return HttpResponse.json({ notice_version: 'v0-pendiente', newly_accepted: true });
    }),
  );
}

describe('navegación de la sesión en el armazón', { timeout: 60_000 }, () => {
  // Sin StrictMode, como en producción: con él, el arnés puede arrancar el enrutador reconstruido
  // en la ubicación del gemelo que StrictMode construyó y nunca montó (`support/appShell.tsx`), que
  // se quedó en `/other`; el navegador real arranca donde está `window.location`.
  it.each([['como en producción (sin StrictMode)', false]])(
    '%s: aceptar el aviso en la misma ruta no deja navegación pendiente para la siguiente reconstrucción',
    async (_, strict) => {
      const state: State = { noticeAccepted: true, permissions: PERMISSIONS };
      serve(state);
      const fixture = mountShell('/other', SCREENS, { strict });
      const user = userEvent.setup();
      await screen.findByRole('heading', { level: 1, name: 'Otra sintética' }, WAIT);
      // A mitad de sesión se publica un aviso nuevo: cualquier respuesta lo exige.
      state.noticeAccepted = false;
      await act(async () => {
        await fixture.services.session.load().catch(() => undefined);
      });
      await screen.findByRole('heading', { level: 1, name: text('scr01.privacy.title') }, WAIT);
      expect(fixture.services.session.getSnapshot().return_route).toBe('/other');
      await user.click(screen.getByRole('button', { name: text('scr01.privacy.accept') }));
      await screen.findByRole('heading', { level: 1, name: 'Otra sintética' }, WAIT);
      expect(fixture.route()).toBe('/other');
      const link = document.querySelector<HTMLAnchorElement>('nav a[href="/findings"]');
      if (link === null) throw new Error('Falta el enlace del menú a /findings.');
      await user.click(link);
      await screen.findByRole('heading', { level: 1, name: 'Inicio sintético' }, WAIT);
      expect(fixture.route()).toBe('/findings');
      // Otros permisos: `AppRouter` construye un enrutador nuevo, que no vuelve a `/other`.
      state.permissions = [...PERMISSIONS, 'actions.manage'];
      await act(async () => {
        await fixture.services.session.load();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 300));
      });
      expect({
        route: fixture.route(),
        heading: screen.queryByRole('heading', { level: 1 })?.textContent,
      }).toEqual({ route: '/findings', heading: 'Inicio sintético' });
    },
  );

  it('el puente pasa replace a la navegación del enrutador montado', async () => {
    serve({ noticeAccepted: true, permissions: PERMISSIONS });
    const fixture = mountShell('/findings', SCREENS, { strict: false });
    await screen.findByRole('heading', { level: 1, name: 'Inicio sintético' }, WAIT);
    act(() => {
      fixture.services.session.navigate('/other', { replace: true });
    });
    await waitFor(() => {
      expect(fixture.route()).toBe('/other');
    }, WAIT);
    expect(fixture.router()?.state.historyAction).toBe('REPLACE');
    act(() => {
      fixture.services.session.navigate('/findings');
    });
    await waitFor(() => {
      expect(fixture.route()).toBe('/findings');
    }, WAIT);
    expect(fixture.router()?.state.historyAction).toBe('PUSH');
  });
});
