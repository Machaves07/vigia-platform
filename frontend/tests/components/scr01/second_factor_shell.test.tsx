/**
 * O-13 (recorrido de cierre M10 de U-06): tras el código correcto del segundo factor, la persona
 * entraba (menú y notificaciones a la vista) pero el centro seguía pintando la tarjeta «Segundo
 * factor de autenticación». El arnés de SCR-01 (`support.tsx`) monta un único enrutador y no lo
 * reproduce: aquí va el armazón real (`App`, `AppRouter`, `SessionGate`, `ShellLayout`), que
 * reconstruye el enrutador al llegar el `Principal`, con MSW y datos sintéticos.
 *
 * Causa: `GET /me` cambia el `Principal`, `AppRouter` construye un enrutador nuevo en la ruta en que
 * estaba el anterior (`/login/second-factor`) y `completeSignIn` navegaba con el `navigate` del
 * enrutador ya descartado, así que la navegación no llegaba nunca a la pantalla.
 */
import { screen, waitFor } from '@testing-library/react';
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

// La campana real es una consulta viva; aquí solo importa la entrada al armazón.
vi.mock('@/pages/scr15/Bell', () => ({
  Bell: () => <Link to="/notifications">{text('navigation.bell')}</Link>,
}));

const EMAIL = 'persona@ejemplo.test';
const PASSWORD = 'clave-sintetica-larga';
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
  ...APP_SCREENS.filter((descriptor) => descriptor.screen_id === 'SCR-01'),
];

/** Sin sesión hasta que el segundo factor responde `authenticated`; después, `GET /me`. */
function serveTwoStepSignIn(firstStep: 'second_factor_required' | 'authenticated') {
  let signedIn = false;
  const calls: string[] = [];
  server.use(
    http.get(`${ORIGIN}/me`, () => {
      calls.push('me');
      return signedIn
        ? HttpResponse.json(shellMe(PERMISSIONS), {
            headers: { 'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION },
          })
        : apiError('unauthenticated', 401);
    }),
    http.get(`${ORIGIN}/hierarchy`, () =>
      signedIn ? HttpResponse.json(shellHierarchy()) : apiError('unauthenticated', 401),
    ),
    http.post(`${ORIGIN}/auth/login`, () => {
      calls.push('login');
      if (firstStep === 'authenticated') signedIn = true;
      return HttpResponse.json({ status: firstStep });
    }),
    http.post(`${ORIGIN}/auth/second-factor`, () => {
      calls.push('second_factor');
      signedIn = true;
      return HttpResponse.json({ status: 'authenticated' });
    }),
  );
  return calls;
}

async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(text('scr01.login.email'), {}, WAIT), EMAIL);
  await user.type(screen.getByLabelText(text('scr01.login.password')), PASSWORD);
  await user.click(screen.getByRole('button', { name: text('scr01.login.submit') }));
}

/** Lo que la persona ve tras entrar: ruta, título de la pantalla, armazón y tarjeta del código. */
function entered(fixture: ReturnType<typeof mountShell>) {
  return {
    route: fixture.route(),
    heading: screen.queryByRole('heading', { level: 1, name: 'Inicio sintético' })?.textContent,
    shell: screen.queryByRole('navigation', { name: text('app_shell.nav.label') }) !== null,
    secondFactorCard:
      screen.queryByRole('heading', { name: text('scr01.second_factor.title') }) !== null,
    codeField: screen.queryByLabelText(text('scr01.second_factor.code')) !== null,
  };
}

describe('O-13: el segundo factor lleva a la pantalla de inicio', { timeout: 60_000 }, () => {
  // Sin StrictMode, como en la construcción de producción, el enrutador nuevo arranca donde está
  // el navegador (`/login/second-factor`): es justo lo que vio el dueño. Con StrictMode el arnés
  // puede arrancarlo en `/login`; en los dos casos la persona no llegaba al inicio.
  it.each([
    ['como en producción (sin StrictMode)', false],
    ['con StrictMode', true],
  ])(
    '%s: login → second_factor_pending → código correcto → GET /me: inicio sin la tarjeta del código',
    async (_, strict) => {
      const calls = serveTwoStepSignIn('second_factor_required');
      const fixture = mountShell('/login', SCREENS, { strict });
      const user = userEvent.setup();
      await signIn(user);
      await user.type(
        await screen.findByLabelText(text('scr01.second_factor.code'), {}, WAIT),
        '123456',
      );
      expect(fixture.route()).toBe('/login/second-factor');
      expect(fixture.services.session.getSnapshot().status).toBe('second_factor_pending');
      await user.click(screen.getByRole('button', { name: text('scr01.second_factor.submit') }));
      await screen.findByRole('navigation', { name: text('app_shell.nav.label') }, WAIT);
      await waitFor(() => {
        expect(entered(fixture)).toEqual({
          route: '/findings',
          heading: 'Inicio sintético',
          shell: true,
          secondFactorCard: false,
          codeField: false,
        });
      }, WAIT);
      expect(fixture.services.session.getSnapshot().status).toBe('active');
      expect(calls).toEqual(['me', 'login', 'second_factor', 'me']);
    },
  );

  it('el mismo camino sin segundo factor (login autenticado) también llega al inicio', async () => {
    serveTwoStepSignIn('authenticated');
    const fixture = mountShell('/login', SCREENS);
    await signIn(userEvent.setup());
    await screen.findByRole('navigation', { name: text('app_shell.nav.label') }, WAIT);
    await waitFor(() => {
      expect(entered(fixture)).toEqual({
        route: '/findings',
        heading: 'Inicio sintético',
        shell: true,
        secondFactorCard: false,
        codeField: false,
      });
    }, WAIT);
    expect(screen.queryByRole('heading', { name: text('scr01.login.title') })).toBeNull();
  });
});
