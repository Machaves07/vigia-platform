/**
 * `/login/enroll` (IF-02 desde IF-01; BR-APP-12; NFR-APP-27):
 *
 * - La inscripción se pide una sola vez por visita (también con el doble montaje de StrictMode) y
 *   un código rechazado repite solo la confirmación, sin regenerar el secreto.
 * - El QR del servidor se pinta como SVG de React, con la clave para escribirla a mano; los diez
 *   códigos, una vez, con «lo guardé» obligatorio antes del primer TOTP.
 * - Tras salir de la ruta, ni los códigos de recuperación ni el secreto del QR (los valores
 *   exactos generados) están en el DOM ni en ningún almacenamiento del navegador.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import fc from 'fast-check';
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
  storedText,
  WAIT,
} from './support';

const SYMBOLS = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

/** Inscripción sintética: diez códigos distintos, un secreto base32 y un SVG como el de `qrcode`. */
function syntheticEnrollment(seed: number) {
  const symbols = (alphabet: string, length: number) =>
    fc
      .array(fc.integer({ min: 0, max: alphabet.length - 1 }), {
        minLength: length,
        maxLength: length,
      })
      .map((indexes) => indexes.map((index) => alphabet.charAt(index)).join(''));
  const [sample] = fc.sample(
    fc.record({
      codes: fc.uniqueArray(symbols(SYMBOLS, 10), { minLength: 10, maxLength: 10 }),
      secret: symbols(BASE32, 32),
    }),
    { seed, numRuns: 1 },
  );
  if (sample === undefined) throw new Error('sin muestra');
  const recovery_codes = sample.codes.map((code) => `${code.slice(0, 5)}-${code.slice(5)}`);
  const d = 'M4,4H11V11H4z M13,4H14V5H13z M26,26H33V33H26z';
  return {
    recovery_codes,
    secret: sample.secret,
    d,
    view: {
      provisioning_uri: `otpauth://totp/Vigia:persona%40ejemplo.test?${new URLSearchParams({
        secret: sample.secret,
        issuer: 'Vigia',
        algorithm: 'SHA1',
        digits: '6',
        period: '30',
      }).toString()}`,
      qr_svg: `<svg width="37mm" height="37mm" version="1.1" viewBox="0 0 37 37" xmlns="http://www.w3.org/2000/svg"><path d="${d}" id="qr-path" fill="#000000" fill-opacity="1" fill-rule="nonzero" stroke="none" /></svg>`,
      recovery_codes,
    },
  };
}

function rejected(status: number, code: string) {
  return HttpResponse.json(
    {
      code,
      detail_code: null,
      message_es: 'Rechazo sintético',
      correlation_id: '0190a1b2-0000-7000-8000-0000000000e1',
      retry_after_seconds: null,
    },
    { status },
  );
}

/** MSW de la inscripción: cuenta los inicios y responde a la confirmación con `confirm`. */
function enrollServer(enrollment: ReturnType<typeof syntheticEnrollment>, confirm: () => Response) {
  const calls = { start: 0, confirm: [] as string[] };
  server.use(
    http.post(`${ORIGIN}/auth/second-factor/enroll`, async ({ request }) => {
      const body = (await request.json()) as { code?: string };
      if (body.code === undefined) {
        calls.start += 1;
        return HttpResponse.json({ status: 'enrollment_started', enrollment: enrollment.view });
      }
      calls.confirm.push(body.code);
      return confirm();
    }),
  );
  return calls;
}

function mountEnroll() {
  return mount('/login/enroll', {
    before: (session) => {
      session.secondFactorRequired(true);
    },
  });
}

describe('/login/enroll', { timeout: 60_000 }, () => {
  it('muestra el QR del servidor, la clave y los diez códigos; el código pide «lo guardé»', async () => {
    const enrollment = syntheticEnrollment(4261);
    const calls = enrollServer(enrollment, () => rejected(401, 'unauthenticated'));
    mountEnroll();
    const qr = await screen.findByRole('img', { name: text('scr01.enroll.qr') }, WAIT);
    expect(qr.getAttribute('viewBox')).toBe('0 0 37 37');
    expect(qr.querySelector('path')?.getAttribute('d')).toBe(enrollment.d);
    expect(screen.getByText(enrollment.secret).tagName).toBe('CODE');
    const secret = screen.getByRole('region', {
      name: text('components_forms.secret.recovery_codes'),
    });
    expect(
      within(secret)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(enrollment.recovery_codes);
    expect(screen.queryByLabelText(text('scr01.enroll.code'))).toBeNull();
    expect(calls.start).toBe(1);
  });

  it('códigos y secreto fuera del DOM y de todo almacenamiento tras salir (BR-APP-12)', async () => {
    const enrollment = syntheticEnrollment(4262);
    let accept = false;
    const calls = enrollServer(enrollment, () =>
      accept ? HttpResponse.json({ status: 'authenticated' }) : rejected(401, 'unauthenticated'),
    );
    const me = currentMe();
    server.use(http.get(`${ORIGIN}/me`, () => HttpResponse.json(me)));
    const fixture = mountEnroll();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole(
        'button',
        { name: text('components_forms.secret.acknowledge') },
        WAIT,
      ),
    );
    // «Lo guardé» suelta los códigos: ya no están en el DOM.
    const afterAck = document.body.innerHTML;
    for (const code of enrollment.recovery_codes) expect(afterAck.includes(code)).toBe(false);
    expect(screen.getByRole('status').getAttribute('data-text-key')).toBe(
      'scr01.enroll.codes_saved',
    );

    // Código rechazado: el mismo aviso uniforme, se repite solo la confirmación.
    await user.type(screen.getByLabelText(text('scr01.enroll.code')), '000000');
    await user.click(screen.getByRole('button', { name: text('scr01.enroll.confirm') }));
    expect((await screen.findByRole('alert', {}, WAIT)).getAttribute('data-text-key')).toBe(
      'scr01.invalid_credentials',
    );
    expect(screen.getByRole('img', { name: text('scr01.enroll.qr') })).toBeDefined();

    accept = true;
    await user.type(screen.getByLabelText(text('scr01.enroll.code')), '123456');
    await user.click(screen.getByRole('button', { name: text('scr01.enroll.confirm') }));
    await waitFor(() => {
      expect(location(fixture)).toBe('/home');
    }, WAIT);
    await screen.findByRole('heading', { level: 1, name: 'Inicio sintético' }, WAIT);
    expect(calls.start).toBe(1);
    expect(calls.confirm).toEqual(['000000', '123456']);

    const dom = document.documentElement.outerHTML;
    const stored = storedText();
    for (const value of [...enrollment.recovery_codes, enrollment.secret, enrollment.d]) {
      expect(dom.includes(value), value).toBe(false);
      expect(stored.includes(value), value).toBe(false);
    }
    expect(fixture.session.getSnapshot().status).toBe('active');
  });

  it('si no se puede iniciar, lo dice y solo ofrece volver a iniciar sesión', async () => {
    server.use(
      http.post(`${ORIGIN}/auth/second-factor/enroll`, () => rejected(401, 'unauthenticated')),
    );
    mountEnroll();
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('scr01.enroll.start_failed');
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
      text('scr01.back_to_login'),
    ]);
  });

  it('un SVG sin la forma de qrcode no se pinta: queda la clave a mano', async () => {
    const enrollment = syntheticEnrollment(4263);
    enrollServer(
      {
        ...enrollment,
        view: { ...enrollment.view, qr_svg: '<svg><script>alert(1)</script></svg>' },
      },
      () => rejected(401, 'unauthenticated'),
    );
    mountEnroll();
    expect(await screen.findByText(text('scr01.enroll.qr_unavailable'), {}, WAIT)).toBeDefined();
    expect(screen.queryByRole('img')).toBeNull();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText(enrollment.secret)).toBeDefined();
  });

  it('axe sin violaciones serias con los códigos y con la confirmación', async () => {
    const enrollment = syntheticEnrollment(4264);
    enrollServer(enrollment, () => rejected(401, 'unauthenticated'));
    const fixture = mountEnroll();
    await screen.findByRole('img', { name: text('scr01.enroll.qr') }, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
    const user = userEvent.setup();
    await user.click(
      screen.getByRole('button', { name: text('components_forms.secret.acknowledge') }),
    );
    await screen.findByLabelText(text('scr01.enroll.code'), {}, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
