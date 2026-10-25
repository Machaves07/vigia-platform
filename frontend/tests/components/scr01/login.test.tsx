/**
 * `/login` (IF-01; BR-APP-09, 10, 11 y 42; NFR-APP-27) con el transporte real y MSW:
 *
 * - Correo desconocido, contraseña incorrecta y código incorrecto pintan el mismo árbol y la
 *   misma clave `scr01.invalid_credentials`, aunque el servidor devolviera cuerpos distintos
 *   (`message_es`, `detail_code`, `correlation_id`): se comparan árboles serializados y claves,
 *   nunca subcadenas.
 * - Validación solo estructural: sin petición si el correo no tiene forma o la contraseña tiene
 *   menos de 8 caracteres.
 * - `throttled`: el envío queda deshabilitado exactamente `retry_after_seconds` (reloj simulado).
 * - Las tres respuestas correctas llevan a su único paso siguiente; la sesión completa carga
 *   `GET /me` y vuelve a la ruta guardada.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { server } from '../../msw/server';
import {
  advance,
  currentMe,
  expectNoSerious,
  location,
  mount,
  normalizedTree,
  ORIGIN,
  seriousViolations,
  WAIT,
} from './support';

const EMAIL = 'persona@ejemplo.test';
const PASSWORD = 'clave-sintetica-1';

/** Cuerpo de rechazo válido (`ApiErrorBody`) con los detalles que un servidor indiscreto daría. */
function rejection(
  code: string,
  status: number,
  messageEs: string,
  detailCode: string | null,
  correlationId: string,
) {
  return HttpResponse.json(
    {
      code,
      detail_code: detailCode,
      message_es: messageEs,
      correlation_id: correlationId,
      retry_after_seconds: null,
    },
    { status },
  );
}

/** Tres causas, tres cuerpos distintos: la interfaz no debe distinguirlas. */
const CAUSES = {
  unknown_email: () =>
    rejection(
      'unauthenticated',
      401,
      'No existe ninguna cuenta con ese correo',
      'app_unknown_email',
      '0190a1b2-0000-7000-8000-0000000000c1',
    ),
  wrong_password: () =>
    rejection(
      'unauthenticated',
      401,
      'La contraseña no coincide',
      null,
      '0190a1b2-0000-7000-8000-0000000000c2',
    ),
  wrong_code: () =>
    rejection(
      'unauthenticated',
      401,
      'El código del segundo factor no coincide',
      'app_wrong_code',
      '0190a1b2-0000-7000-8000-0000000000c3',
    ),
} as const;

async function signIn(email = EMAIL, password = PASSWORD) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(text('scr01.login.email'), {}, WAIT), email);
  await user.type(screen.getByLabelText(text('scr01.login.password')), password);
  await user.click(screen.getByRole('button', { name: text('scr01.login.submit') }));
}

/** Rechazo de `/login` por `cause`: el árbol completo del formulario y el aviso. */
async function loginRejected(cause: keyof typeof CAUSES) {
  server.use(http.post(`${ORIGIN}/auth/login`, CAUSES[cause]));
  const fixture = mount('/login');
  await signIn();
  const alert = await screen.findByRole('alert', {}, WAIT);
  const result = {
    tree: normalizedTree(fixture.container),
    alert: normalizedTree(alert),
    key: alert.getAttribute('data-text-key'),
  };
  fixture.unmount();
  return result;
}

/** Rechazo del código en `/login/second-factor`: el aviso. */
async function codeRejected() {
  server.use(http.post(`${ORIGIN}/auth/second-factor`, CAUSES.wrong_code));
  const fixture = mount('/login/second-factor', {
    before: (session) => {
      session.secondFactorRequired(false);
    },
  });
  const user = userEvent.setup();
  await user.type(
    await screen.findByLabelText(text('scr01.second_factor.code'), {}, WAIT),
    '123456',
  );
  await user.click(screen.getByRole('button', { name: text('scr01.second_factor.submit') }));
  const alert = await screen.findByRole('alert', {}, WAIT);
  const result = { alert: normalizedTree(alert), key: alert.getAttribute('data-text-key') };
  fixture.unmount();
  return result;
}

describe('/login', { timeout: 60_000 }, () => {
  it('las tres causas de rechazo pintan el mismo árbol y la misma clave (BR-APP-10)', async () => {
    const unknown = await loginRejected('unknown_email');
    const password = await loginRejected('wrong_password');
    const code = await codeRejected();
    expect(unknown.key).toBe('scr01.invalid_credentials');
    expect(password.key).toBe('scr01.invalid_credentials');
    expect(code.key).toBe('scr01.invalid_credentials');
    expect(password.tree).toBe(unknown.tree);
    expect(password.alert).toBe(unknown.alert);
    expect(code.alert).toBe(unknown.alert);
  });

  it('el aviso es el texto del catálogo y nunca el mensaje ni la correlación del servidor', async () => {
    server.use(http.post(`${ORIGIN}/auth/login`, CAUSES.unknown_email));
    mount('/login');
    await signIn();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.textContent).toBe(text('scr01.invalid_credentials'));
    // La contraseña no sobrevive a un rechazo.
    expect(screen.getByLabelText<HTMLInputElement>(text('scr01.login.password')).value).toBe('');
  });

  it('valida solo la estructura y no envía nada si no la cumple', async () => {
    const requests: string[] = [];
    server.events.on('request:start', ({ request }) => {
      requests.push(request.url);
    });
    try {
      mount('/login');
      await signIn('sin-arroba', 'corta');
      expect(screen.getByText(text('scr01.login.email_invalid'))).toBeDefined();
      expect(screen.getByText(text('scr01.login.password_short'))).toBeDefined();
      expect(screen.getByLabelText(text('scr01.login.email')).getAttribute('aria-invalid')).toBe(
        'true',
      );
      expect(requests).toEqual([]);
    } finally {
      server.events.removeAllListeners('request:start');
    }
  });

  it('throttled deshabilita el envío exactamente retry_after_seconds (BR-APP-11)', async () => {
    let calls = 0;
    server.use(
      http.post(`${ORIGIN}/auth/login`, () => {
        calls += 1;
        return HttpResponse.json(
          {
            code: 'throttled',
            detail_code: null,
            message_es: 'Demasiados intentos',
            correlation_id: '0190a1b2-0000-7000-8000-0000000000c4',
            retry_after_seconds: 30,
          },
          { status: 429, headers: { 'retry-after': '30' } },
        );
      }),
    );
    const fixture = mount('/login');
    await signIn();
    const notice = await screen.findByRole('status', {}, WAIT);
    expect(notice.getAttribute('data-text-key')).toBe('scr01.throttled');
    const submit = screen.getByRole('button', { name: text('scr01.login.submit') });
    expect(submit).toHaveProperty('disabled', true);
    expect(notice.textContent).toBe(`${text('scr01.throttled')} 30 ${text('scr01.seconds')}`);
    // Segundo a segundo, como el navegador: cada tic programa el siguiente.
    for (let second = 0; second < 29; second += 1) advance(fixture.clock, 1_000);
    expect(submit).toHaveProperty('disabled', true);
    expect(screen.getByRole('status').textContent).toBe(
      `${text('scr01.throttled')} 1 ${text('scr01.seconds')}`,
    );
    advance(fixture.clock, 1_000);
    await waitFor(() => {
      expect(submit).toHaveProperty('disabled', false);
    }, WAIT);
    expect(screen.queryByRole('status')).toBeNull();
    expect(calls).toBe(1);
  });

  it('con segundo factor, el único paso siguiente es el código o la inscripción (BR-APP-09)', async () => {
    server.use(
      http.post(`${ORIGIN}/auth/login`, () =>
        HttpResponse.json({ status: 'second_factor_required' }),
      ),
    );
    const code = mount('/login');
    await signIn();
    await waitFor(() => {
      expect(location(code)).toBe('/login/second-factor');
    }, WAIT);
    expect(code.session.getSnapshot().status).toBe('second_factor_pending');
    expect(
      await screen.findByRole(
        'heading',
        { level: 1, name: text('scr01.second_factor.title') },
        WAIT,
      ),
    ).toBeDefined();
    code.unmount();

    server.use(
      http.post(`${ORIGIN}/auth/login`, () =>
        HttpResponse.json({ status: 'second_factor_enrollment_required' }),
      ),
    );
    const enroll = mount('/login');
    await signIn();
    await waitFor(() => {
      expect(location(enroll)).toBe('/login/enroll');
    }, WAIT);
    expect(enroll.session.getSnapshot().second_factor_enrollment_required).toBe(true);
  });

  it('sesión completa: carga GET /me y vuelve al inicio, no otra vez a /login', async () => {
    const me = currentMe();
    server.use(
      http.post(`${ORIGIN}/auth/login`, () => HttpResponse.json({ status: 'authenticated' })),
      http.get(`${ORIGIN}/me`, () => HttpResponse.json(me)),
    );
    const fixture = mount('/login');
    await signIn();
    await waitFor(() => {
      expect(location(fixture)).toBe('/home');
    }, WAIT);
    expect(fixture.session.getSnapshot().status).toBe('active');
    expect(fixture.session.getSnapshot().principal?.user_id).toBe(me.user.user_id);
  });

  it('el envío con Intro también valida y envía una vez', async () => {
    let calls = 0;
    server.use(
      http.post(`${ORIGIN}/auth/login`, () => {
        calls += 1;
        return CAUSES.wrong_password();
      }),
    );
    mount('/login');
    const email = await screen.findByLabelText(text('scr01.login.email'), {}, WAIT);
    fireEvent.change(email, { target: { value: EMAIL } });
    fireEvent.change(screen.getByLabelText(text('scr01.login.password')), {
      target: { value: PASSWORD },
    });
    const form = email.closest('form');
    if (form === null) throw new Error('sin formulario');
    fireEvent.submit(form);
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('scr01.invalid_credentials');
    expect(calls).toBe(1);
  });

  it('axe sin violaciones serias, vacío y con el aviso', async () => {
    server.use(http.post(`${ORIGIN}/auth/login`, CAUSES.wrong_password));
    const fixture = mount('/login');
    await screen.findByLabelText(text('scr01.login.email'), {}, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
    await signIn('sin-arroba', 'corta');
    expectNoSerious(await seriousViolations(fixture.container));
    fixture.unmount();
    const rejected = mount('/login');
    await signIn();
    await screen.findByRole('alert', {}, WAIT);
    expectNoSerious(await seriousViolations(rejected.container));
  });
});
