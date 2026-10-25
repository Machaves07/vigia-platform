/**
 * `/login/second-factor` y la puerta del segundo paso (IF-01 paso 3; BR-APP-09, 10 y 11):
 *
 * - Con el factor sin inscribir según el servidor, cualquier URL (también `/home` y `/login`)
 *   redirige a `/login/enroll`, sobre el árbol real de `createAppRoutes`; con el factor inscrito,
 *   a `/login/second-factor`, y la inscripción tampoco es alcanzable.
 * - TOTP de 6 dígitos o código de recuperación (con guiones al copiarlo); otra forma no se envía.
 * - Código correcto: `GET /me` y vuelta a la ruta guardada; «volver» cierra la sesión pendiente.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { server } from '../../msw/server';
import {
  currentMe,
  expectNoSerious,
  location,
  mount,
  ORIGIN,
  seriousViolations,
  WAIT,
} from './support';

async function typeCode(code: string) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(text('scr01.second_factor.code'), {}, WAIT), code);
  await user.click(screen.getByRole('button', { name: text('scr01.second_factor.submit') }));
}

describe('segundo paso del inicio de sesión', { timeout: 60_000 }, () => {
  it.each(['/home', '/login', '/login/second-factor', '/admin/users', '/esta-no-existe'])(
    'con el factor sin inscribir, %s por URL redirige a /login/enroll (BR-APP-09)',
    async (path) => {
      const fixture = mount(path, {
        routes: 'app',
        before: (session) => {
          session.secondFactorRequired(true);
        },
      });
      await waitFor(() => {
        expect(location(fixture)).toBe('/login/enroll');
      }, WAIT);
      expect(
        await screen.findByRole('heading', { level: 1, name: text('scr01.enroll.title') }, WAIT),
      ).toBeDefined();
    },
  );

  it('con el factor inscrito, /home y /login/enroll redirigen al código', async () => {
    for (const path of ['/home', '/login/enroll']) {
      const fixture = mount(path, {
        routes: 'app',
        before: (session) => {
          session.secondFactorRequired(false);
        },
      });
      await waitFor(() => {
        expect(location(fixture)).toBe('/login/second-factor');
      }, WAIT);
      expect(
        await screen.findByRole(
          'heading',
          { level: 1, name: text('scr01.second_factor.title') },
          WAIT,
        ),
      ).toBeDefined();
      fixture.unmount();
    }
  });

  it('sin sesión pendiente, /home sigue siendo «no disponible en su alcance»', async () => {
    const fixture = mount('/home', { routes: 'app' });
    expect(
      await screen.findByRole(
        'heading',
        { level: 1, name: text('errors.not_available_in_scope') },
        WAIT,
      ),
    ).toBeDefined();
    expect(location(fixture)).toBe('/home');
  });

  it('acepta TOTP y código de recuperación; otra forma no se envía', async () => {
    const sent: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/auth/second-factor`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(
          {
            code: 'unauthenticated',
            detail_code: null,
            message_es: 'Código incorrecto',
            correlation_id: '0190a1b2-0000-7000-8000-0000000000d1',
            retry_after_seconds: null,
          },
          { status: 401 },
        );
      }),
    );
    const fixture = mount('/login/second-factor', {
      before: (session) => {
        session.secondFactorRequired(false);
      },
    });
    await typeCode('12a456');
    expect(screen.getByText(text('scr01.second_factor.code_invalid'))).toBeDefined();
    expect(sent).toEqual([]);
    fixture.unmount();

    for (const code of ['654321', 'ABCDE-23456', ' abcde 23456 ']) {
      const again = mount('/login/second-factor', {
        before: (session) => {
          session.secondFactorRequired(false);
        },
      });
      await typeCode(code);
      const alert = await screen.findByRole('alert', {}, WAIT);
      expect(alert.getAttribute('data-text-key')).toBe('scr01.invalid_credentials');
      again.unmount();
    }
    expect(sent).toEqual([{ code: '654321' }, { code: 'ABCDE-23456' }, { code: 'abcde 23456' }]);
  });

  it('código correcto: carga GET /me y vuelve a la ruta guardada del bloqueo', async () => {
    const me = currentMe();
    server.use(
      http.post(`${ORIGIN}/auth/second-factor`, () =>
        HttpResponse.json({ status: 'authenticated' }),
      ),
      http.get(`${ORIGIN}/me`, () => HttpResponse.json(me)),
    );
    const fixture = mount('/login/second-factor', {
      before: (session) => {
        session.secondFactorRequired(false);
      },
    });
    await typeCode('123456');
    await waitFor(() => {
      expect(location(fixture)).toBe('/home');
    }, WAIT);
    expect(fixture.session.getSnapshot().status).toBe('active');
  });

  it('«volver» cierra la sesión pendiente en el servidor y vuelve a /login', async () => {
    let logouts = 0;
    server.use(
      http.post(`${ORIGIN}/auth/logout`, () => {
        logouts += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const fixture = mount('/login/second-factor', {
      before: (session) => {
        session.secondFactorRequired(false);
      },
    });
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', { name: text('scr01.back_to_login') }, WAIT),
    );
    await waitFor(() => {
      expect(location(fixture)).toBe('/login');
    }, WAIT);
    expect(logouts).toBe(1);
    expect(fixture.session.getSnapshot().status).toBe('anonymous');
    expect(
      await screen.findByRole('heading', { level: 1, name: text('scr01.login.title') }, WAIT),
    ).toBeDefined();
  });

  it('axe sin violaciones serias', async () => {
    const fixture = mount('/login/second-factor', {
      before: (session) => {
        session.secondFactorRequired(false);
      },
    });
    await screen.findByLabelText(text('scr01.second_factor.code'), {}, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
    await typeCode('x');
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
