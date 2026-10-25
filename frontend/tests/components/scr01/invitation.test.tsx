/**
 * `/invitations/{token}` (IF-04; BR-APP-18, BR-APP-12; NFR-APP-27 y 33):
 *
 * - Un enlace usado, vencido o inexistente (`not_found`, con cuerpos distintos) pinta el mismo
 *   árbol y la misma clave `scr01.invitation.invalid`, también si el enlace deja de valer al
 *   activar.
 * - Flujo por pasos: contraseña con estructura, segundo factor si el servidor lo exige (QR,
 *   códigos una vez, primer TOTP) y aceptación del aviso con su versión; `complete` lleva la
 *   contraseña, la versión y el código.
 * - `begin` se pide una sola vez (StrictMode); el token no queda en ningún almacenamiento.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';

import { server } from '../../msw/server';
import {
  expectNoSerious,
  mount,
  normalizedTree,
  ORIGIN,
  seriousViolations,
  storedText,
  WAIT,
} from './support';

/** Tokens sintéticos armados en ejecución (sin literales con forma de secreto). */
const USED = ['enlace', 'usado', 'sintetico', '0001'].join('-');
const MISSING = ['enlace', 'inexistente', 'sintetico', '0002'].join('-');
const VALID = ['enlace', 'valido', 'sintetico', '0003'].join('-');

const NOTICE = {
  version: 'v0-pendiente',
  text: '# Aviso sintético\n\nTexto marcador del aviso para las pruebas.',
  pending_legal_text: true,
};
const CODES = Array.from({ length: 10 }, (_, index) => `PRUEB-${String(index).padStart(5, '0')}`);

function notFound(messageEs: string, detailCode: string | null, correlation: string) {
  return HttpResponse.json(
    {
      code: 'not_found',
      detail_code: detailCode,
      message_es: messageEs,
      correlation_id: correlation,
      retry_after_seconds: null,
    },
    { status: 404 },
  );
}

/** Rechazos de `begin` según el token: cada causa, un cuerpo distinto. */
function invitationServer(onComplete?: (body: unknown) => Response) {
  const calls = { begin: 0, complete: [] as unknown[] };
  server.use(
    http.post(`${ORIGIN}/invitations/:token/accept`, async ({ params, request }) => {
      const body = (await request.json()) as { step: string };
      if (params.token === USED) {
        return notFound(
          'El enlace ya se usó',
          'app_invitation_used',
          '0190a1b2-0000-7000-8000-0000000001a1',
        );
      }
      if (params.token === MISSING) {
        return notFound('No existe', null, '0190a1b2-0000-7000-8000-0000000001a2');
      }
      if (body.step === 'begin') {
        calls.begin += 1;
        return HttpResponse.json({
          status: 'pending',
          second_factor_required: true,
          enrollment: {
            provisioning_uri: 'otpauth://totp/Vigia:alta?issuer=Vigia',
            qr_svg:
              '<svg viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg"><path d="M0,0H7V7H0z" /></svg>',
            recovery_codes: CODES,
          },
          notice: NOTICE,
        });
      }
      calls.complete.push(body);
      return onComplete?.(body) ?? HttpResponse.json({ status: 'activated' });
    }),
  );
  return calls;
}

async function invalidTree(token: string) {
  const fixture = mount(`/invitations/${token}`);
  const alert = await screen.findByRole('alert', {}, WAIT);
  const result = {
    key: alert.getAttribute('data-text-key'),
    tree: normalizedTree(fixture.container),
  };
  fixture.unmount();
  return result;
}

/** Recorre los pasos hasta el resumen. */
async function walk(user: ReturnType<typeof userEvent.setup>, password = 'clave-nueva-sintetica') {
  await user.type(
    await screen.findByLabelText(text('scr01.invitation.password'), {}, WAIT),
    password,
  );
  await user.type(screen.getByLabelText(text('scr01.invitation.password_confirm')), password);
  await user.click(screen.getByRole('button', { name: text('components_structure.wizard.next') }));
  const secret = await screen.findByRole(
    'region',
    { name: text('components_forms.secret.recovery_codes') },
    WAIT,
  );
  expect(
    within(secret)
      .getAllByRole('listitem')
      .map((item) => item.textContent),
  ).toEqual(CODES);
  expect(screen.queryByLabelText(text('scr01.enroll.code'))).toBeNull();
  await user.click(
    screen.getByRole('button', { name: text('components_forms.secret.acknowledge') }),
  );
  await user.type(screen.getByLabelText(text('scr01.enroll.code')), '123456');
  await user.click(screen.getByRole('button', { name: text('components_structure.wizard.next') }));
  expect(await screen.findByText(NOTICE.version, {}, WAIT)).toBeDefined();
  expect(screen.getByRole('heading', { level: 2, name: 'Aviso sintético' })).toBeDefined();
  await user.click(screen.getByRole('checkbox', { name: text('scr01.invitation.notice_accept') }));
  await user.click(screen.getByRole('button', { name: text('components_structure.wizard.next') }));
}

describe('/invitations/{token}', { timeout: 60_000 }, () => {
  it('un enlace usado y uno inexistente pintan el mismo árbol y la misma clave (BR-APP-18)', async () => {
    invitationServer();
    const used = await invalidTree(USED);
    const missing = await invalidTree(MISSING);
    expect(used.key).toBe('scr01.invitation.invalid');
    expect(missing.key).toBe('scr01.invitation.invalid');
    expect(used.tree).toBe(missing.tree);
  });

  it('activación completa por pasos: contraseña, segundo factor y aviso', async () => {
    const calls = invitationServer();
    const fixture = mount(`/invitations/${VALID}`);
    const user = userEvent.setup();
    await walk(user);
    await user.click(
      await screen.findByRole('button', { name: text('components_structure.wizard.submit') }, WAIT),
    );
    const done = await screen.findByText(text('scr01.invitation.activated'), {}, WAIT);
    expect(done.getAttribute('data-text-key')).toBe('scr01.invitation.activated');
    expect(calls.begin).toBe(1);
    expect(calls.complete).toEqual([
      {
        step: 'complete',
        password: 'clave-nueva-sintetica',
        notice_version: 'v0-pendiente',
        second_factor_code: '123456',
      },
    ]);
    const dom = document.documentElement.outerHTML;
    for (const code of CODES) expect(dom.includes(code), code).toBe(false);
    expect(storedText().includes(VALID)).toBe(false);
    expect(fixture.session.getSnapshot().status).toBe('anonymous');
  });

  it('la contraseña corta o distinta no deja avanzar', async () => {
    invitationServer();
    mount(`/invitations/${VALID}`);
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText(text('scr01.invitation.password'), {}, WAIT),
      'corta',
    );
    expect(screen.getByText(text('scr01.login.password_short'))).toBeDefined();
    const next = screen.getByRole('button', { name: text('components_structure.wizard.next') });
    expect(next).toHaveProperty('disabled', true);
    await user.clear(screen.getByLabelText(text('scr01.invitation.password')));
    await user.type(screen.getByLabelText(text('scr01.invitation.password')), 'clave-larga-uno');
    await user.type(
      screen.getByLabelText(text('scr01.invitation.password_confirm')),
      'clave-larga-dos',
    );
    expect(screen.getByText(text('scr01.invitation.password_mismatch'))).toBeDefined();
    expect(next).toHaveProperty('disabled', true);
  });

  it('si el enlace deja de valer al activar, el mismo mensaje del catálogo', async () => {
    invitationServer(() =>
      notFound(
        'Vencido entretanto',
        'app_invitation_expired',
        '0190a1b2-0000-7000-8000-0000000001a3',
      ),
    );
    const fixture = mount(`/invitations/${VALID}`);
    const user = userEvent.setup();
    await walk(user);
    await user.click(
      await screen.findByRole('button', { name: text('components_structure.wizard.submit') }, WAIT),
    );
    const alert = await screen.findByRole('alert', {}, WAIT);
    await waitFor(() => {
      expect(alert.getAttribute('data-text-key')).toBe('scr01.invitation.invalid');
    }, WAIT);
    const late = normalizedTree(fixture.container);
    fixture.unmount();
    expect(late).toBe((await invalidTree(MISSING)).tree);
  });

  it('una contraseña o un código rechazados lo dicen sin perder el flujo', async () => {
    invitationServer(() =>
      HttpResponse.json(
        {
          code: 'invalid_request',
          detail_code: null,
          message_es: 'Contraseña filtrada',
          correlation_id: '0190a1b2-0000-7000-8000-0000000001a4',
          retry_after_seconds: null,
        },
        { status: 400 },
      ),
    );
    mount(`/invitations/${VALID}`);
    const user = userEvent.setup();
    await walk(user);
    await user.click(
      await screen.findByRole('button', { name: text('components_structure.wizard.submit') }, WAIT),
    );
    const alerts = await screen.findAllByRole('alert', {}, WAIT);
    expect(alerts.map((alert) => alert.getAttribute('data-text-key'))).toContain(
      'scr01.invitation.rejected',
    );
    expect(
      screen.getByRole('button', { name: text('components_structure.wizard.back') }),
    ).toHaveProperty('disabled', false);
  });

  it('axe sin violaciones serias en el flujo y en el enlace no válido', async () => {
    invitationServer();
    const fixture = mount(`/invitations/${VALID}`);
    await screen.findByLabelText(text('scr01.invitation.password'), {}, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(text('scr01.invitation.password')), 'clave-nueva-1');
    await user.type(
      screen.getByLabelText(text('scr01.invitation.password_confirm')),
      'clave-nueva-1',
    );
    await user.click(
      screen.getByRole('button', { name: text('components_structure.wizard.next') }),
    );
    await screen.findByRole(
      'region',
      { name: text('components_forms.secret.recovery_codes') },
      WAIT,
    );
    expectNoSerious(await seriousViolations(fixture.container));
    fixture.unmount();
    const invalid = mount(`/invitations/${USED}`);
    await screen.findByRole('alert', {}, WAIT);
    expectNoSerious(await seriousViolations(invalid.container));
  });
});
