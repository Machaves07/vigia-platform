/**
 * `/account` (RV-01; IF-02 e IF-04; BR-APP-17; PAT-APP-MAN-01):
 *
 * - Datos de la cuenta con los nombres de los alcances que da `GET /hierarchy`.
 * - Sesiones con `client_hint`, fechas y estado, nunca un identificador aunque el servidor lo
 *   enviara; «cerrar las demás» informa cuántas cerró.
 * - Cambio de contraseña con validación de estructura; el `invalid_request` del servidor se ancla
 *   al formulario y conserva lo escrito; aceptado, borra los campos y avisa que las demás
 *   sesiones quedaron cerradas.
 * - Las tres versiones con su etiqueta, comprobadas por clave del catálogo; `axe` sin
 *   violaciones serias.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';
import { label } from '@/texts/labels';

import platform from '../../../../backend/resources/labels.platform.es.json';
import { server } from '../../msw/server';
import { apiError, mountAt, nth, ORGANIZATION, ORIGIN, PLANT, WAIT } from './admin_support';
import { expectNoSerious, seriousViolations } from './support';

/** Valor sintético con forma de identificador de sesión que el servidor nunca debe mandar. */
const LEAKED_SESSION_ID = '0190a1b2-5e55-7000-8000-00000000c0de';

/** Agente de usuario sintético: la pantalla lo resume como «Chrome en Windows». */
const SYNTHETIC_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';

function sessionsServer() {
  const calls = { closeOthers: 0 };
  let closed = false;
  server.use(
    http.get(`${ORIGIN}/auth/sessions`, () =>
      HttpResponse.json({
        sessions: [
          {
            created_at: '2026-01-01T08:00:00.000Z',
            last_seen_at: '2026-01-01T11:59:00.000Z',
            client_hint: SYNTHETIC_AGENT,
            current: true,
            usable: true,
            session_id: LEAKED_SESSION_ID,
          },
          ...(closed
            ? []
            : [
                {
                  created_at: '2025-12-31T08:00:00.000Z',
                  last_seen_at: '2025-12-31T09:00:00.000Z',
                  client_hint: null,
                  current: false,
                  usable: false,
                },
              ]),
        ],
      }),
    ),
    http.post(`${ORIGIN}/auth/sessions/close-others`, () => {
      calls.closeOthers += 1;
      closed = true;
      return HttpResponse.json({ sessions_closed: 1 });
    }),
  );
  return calls;
}

describe('/account', { timeout: 60_000 }, () => {
  it('muestra nombre, correo, organización, segundo factor y asignaciones con el nombre del alcance', async () => {
    sessionsServer();
    const fixture = await mountAt('/account', {
      me: {
        assignments: [
          { role: 'administrator', scope_level: 'organization', scope_id: ORGANIZATION },
          { role: 'coordinator_sst', scope_level: 'plant', scope_id: PLANT },
        ],
      },
    });
    const profile = await screen.findByRole(
      'region',
      { name: text('scr01_admin.account.profile') },
      WAIT,
    );
    expect(within(profile).getByText(fixture.me.user.display_name)).toBeDefined();
    expect(within(profile).getByText(fixture.me.user.email)).toBeDefined();
    expect(
      within(profile).getByText(text('scr01_admin.account.second_factor_enrolled')),
    ).toBeDefined();
    await waitFor(() => {
      expect(
        within(profile)
          .getAllByRole('listitem')
          .map((item) => item.textContent),
      ).toEqual([
        `${label('role', 'administrator')} · ${label('scope_level', 'organization')}: Organización sintética`,
        `${label('role', 'coordinator_sst')} · ${label('scope_level', 'plant')}: Planta sintética`,
      ]);
    }, WAIT);
  });

  it('lista las sesiones sin identificador y «cerrar las demás» informa cuántas cerró', async () => {
    const calls = sessionsServer();
    const user = userEvent.setup();
    const fixture = await mountAt('/account');
    const table = await screen.findByRole(
      'table',
      { name: text('scr01_admin.sessions.list') },
      WAIT,
    );
    await waitFor(() => {
      expect(within(table).getAllByRole('row')).toHaveLength(3);
    }, WAIT);
    const rows = within(table).getAllByRole('row').slice(1);
    // El agente de usuario se resume («Chrome en Windows»); la cadena cruda no se muestra.
    expect(
      within(nth(rows, 0)).getByText(`Chrome ${text('scr01_admin.sessions.on')} Windows`),
    ).toBeDefined();
    expect(fixture.container.textContent).not.toContain(SYNTHETIC_AGENT);
    expect(within(nth(rows, 0)).getByText(text('scr01_admin.sessions.current'))).toBeDefined();
    expect(within(nth(rows, 1)).getByText(text('scr01_admin.sessions.no_hint'))).toBeDefined();
    expect(
      within(nth(rows, 1)).getByText(text('scr01_admin.sessions.pending_second_factor')),
    ).toBeDefined();
    // Ni en el texto ni en atributos: el identificador no existe para la interfaz.
    expect(fixture.container.innerHTML.indexOf(LEAKED_SESSION_ID)).toBe(-1);

    await user.click(
      screen.getByRole('button', { name: text('scr01_admin.sessions.close_others') }),
    );
    const status = await screen.findByText(`${text('scr01_admin.sessions.closed')} 1`, {}, WAIT);
    expect(status.getAttribute('data-part')).toBe('sessions_closed');
    expect(calls.closeOthers).toBe(1);
    await waitFor(() => {
      expect(within(table).getAllByRole('row')).toHaveLength(2);
    }, WAIT);
  });

  it('valida la contraseña nueva antes de enviar y no llama a la API', async () => {
    sessionsServer();
    let posted = 0;
    server.use(
      http.post(`${ORIGIN}/auth/password`, () => {
        posted += 1;
        return HttpResponse.json({ sessions_closed: 0 });
      }),
    );
    const user = userEvent.setup();
    await mountAt('/account');
    const form = await screen.findByRole(
      'form',
      { name: text('scr01_admin.password.title') },
      WAIT,
    );
    const submit = within(form).getByRole('button', { name: text('scr01_admin.password.submit') });
    await user.click(submit);
    expect(within(form).getByRole('alert').getAttribute('data-text-key')).toBe(
      'scr01_admin.password.required',
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.current')),
      'actual-sintetica',
    );
    await user.type(within(form).getByLabelText(text('scr01_admin.password.new')), 'corta7!');
    await user.click(submit);
    expect(within(form).getByRole('alert').getAttribute('data-text-key')).toBe(
      'scr01_admin.password.short',
    );
    await user.type(within(form).getByLabelText(text('scr01_admin.password.new')), 'x');
    await user.type(within(form).getByLabelText(text('scr01_admin.password.confirm')), 'distinta8');
    await user.click(submit);
    expect(within(form).getByRole('alert').getAttribute('data-text-key')).toBe(
      'scr01_admin.password.mismatch',
    );
    expect(posted).toBe(0);
  });

  it('un rechazo del servidor se ancla al formulario con su mensaje y conserva lo escrito', async () => {
    sessionsServer();
    server.use(
      http.post(`${ORIGIN}/auth/password`, () =>
        apiError('invalid_request', 422, platform.api_error_code.invalid_request),
      ),
    );
    const user = userEvent.setup();
    await mountAt('/account');
    const form = await screen.findByRole(
      'form',
      { name: text('scr01_admin.password.title') },
      WAIT,
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.current')),
      'actual-sintetica',
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.new')),
      'nueva-sintetica-1',
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.confirm')),
      'nueva-sintetica-1',
    );
    await user.click(
      within(form).getByRole('button', { name: text('scr01_admin.password.submit') }),
    );
    const notice = await within(form).findByRole('alert', {}, WAIT);
    expect(notice.getAttribute('data-code')).toBe('invalid_request');
    expect(within(notice).getByText(platform.api_error_code.invalid_request)).toBeDefined();
    expect(form.getAttribute('aria-describedby')).toBe(notice.id);
    expect(
      within(form).getByLabelText<HTMLInputElement>(text('scr01_admin.password.new')).value,
    ).toBe('nueva-sintetica-1');
    expect(
      within(form).getByLabelText<HTMLInputElement>(text('scr01_admin.password.current')).value,
    ).toBe('actual-sintetica');
  });

  it('al cambiar la contraseña borra los campos y avisa que las demás sesiones quedaron cerradas (BR-APP-17)', async () => {
    sessionsServer();
    const bodies: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/auth/password`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ sessions_closed: 2 });
      }),
    );
    const user = userEvent.setup();
    await mountAt('/account');
    const form = await screen.findByRole(
      'form',
      { name: text('scr01_admin.password.title') },
      WAIT,
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.current')),
      'actual-sintetica',
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.new')),
      'nueva-sintetica-1',
    );
    await user.type(
      within(form).getByLabelText(text('scr01_admin.password.confirm')),
      'nueva-sintetica-1',
    );
    await user.click(
      within(form).getByRole('button', { name: text('scr01_admin.password.submit') }),
    );
    const status = await within(form).findByRole('status', {}, WAIT);
    expect(status.getAttribute('data-text-key')).toBe('scr01_admin.password.changed');
    expect(bodies).toEqual([
      { current_password: 'actual-sintetica', new_password: 'nueva-sintetica-1' },
    ]);
    for (const field of ['current', 'new', 'confirm']) {
      expect(
        within(form).getByLabelText<HTMLInputElement>(text(`scr01_admin.password.${field}`)).value,
      ).toBe('');
    }
  });

  it('muestra las tres versiones con su etiqueta, por clave del catálogo (PAT-APP-MAN-01)', async () => {
    sessionsServer();
    const fixture = await mountAt('/account', { me: { api_version: '7.3.1-sintetica' } });
    const section = await screen.findByRole(
      'region',
      { name: text('scr01_admin.versions.title') },
      WAIT,
    );
    const row = (key: string) => {
      const element = section.querySelector(`[data-text-key="${key}"]`);
      if (!(element instanceof HTMLElement)) throw new Error(`falta la fila ${key}`);
      return {
        label: element.querySelector('dt')?.textContent,
        value: element.querySelector('dd')?.textContent,
      };
    };
    expect(row('scr01_admin.versions.app')).toEqual({
      label: text('scr01_admin.versions.app'),
      // Sin etiqueta de publicación la construcción es `dev` y se dice en palabras.
      value:
        import.meta.env.VITE_APP_VERSION === 'dev'
          ? text('scr01_admin.versions.unreleased')
          : import.meta.env.VITE_APP_VERSION,
    });
    expect(row('scr01_admin.versions.api')).toEqual({
      label: text('scr01_admin.versions.api'),
      value: fixture.me.api_version,
    });
    await waitFor(() => {
      expect(row('scr01_admin.versions.contract')).toEqual({
        label: text('scr01_admin.versions.contract'),
        value: import.meta.env.VITE_CONTRACT_TAG,
      });
    }, WAIT);
  });

  it('sin /version.json la versión del contrato queda «no disponible»', async () => {
    sessionsServer();
    server.use(http.get('/version.json', () => new HttpResponse(null, { status: 404 })));
    await mountAt('/account');
    const section = await screen.findByRole(
      'region',
      { name: text('scr01_admin.versions.title') },
      WAIT,
    );
    await waitFor(() => {
      expect(
        section.querySelector('[data-text-key="scr01_admin.versions.contract"] dd')?.textContent,
      ).toBe(text('scr01_admin.versions.unavailable'));
    }, WAIT);
  });

  it('axe: sin violaciones serias con la cuenta cargada', async () => {
    sessionsServer();
    const fixture = await mountAt('/account');
    await screen.findByRole('table', { name: text('scr01_admin.sessions.list') }, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
