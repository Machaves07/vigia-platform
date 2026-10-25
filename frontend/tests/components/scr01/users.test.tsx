/**
 * `/admin/users` (RV-02; IF-02 paso 4; BR-NUC-30 a 33 decididas por el servidor):
 *
 * - Lista por clave con estado, licencia y asignaciones; sin usuarios, el texto propio.
 * - Invitación: el cuerpo lleva `disclose_link` y el enlace se muestra **una vez** con su valor
 *   exacto; no queda en la caché de mutaciones y desaparece al salir de la pantalla.
 * - El `conflict` del último administrador (al retirar el rol o al desactivar) se ancla al
 *   formulario que lo provocó con el mensaje del servidor, y lo escrito en el panel se conserva.
 * - El restablecimiento del segundo factor exige motivo: sin él no hay petición.
 * - `axe` sin violaciones serias.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { act } from 'react';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';
import { label } from '@/texts/labels';

import platform from '../../../../backend/resources/labels.platform.es.json';
import { server } from '../../msw/server';
import {
  ADMIN_ASSIGNMENT,
  apiError,
  managedUser,
  mountAt,
  nth,
  ORGANIZATION,
  ORIGIN,
  PLANT,
  USER_B,
  WAIT,
} from './admin_support';
import { expectNoSerious, seriousViolations } from './support';

/** Enlace sintético armado en ejecución (sin literales con forma de secreto). */
const LINK = `${ORIGIN}/invitations/${['enlace', 'invitacion', 'sintetico', '0427'].join('-')}`;
const CONFLICT = platform.api_error_code.conflict;

type User = ReturnType<typeof userEvent.setup>;

async function openPanel(user: User) {
  await user.click(
    await screen.findByRole(
      'button',
      { name: `${text('scr01_admin.users.manage')}: Cuenta sintética B` },
      WAIT,
    ),
  );
  return screen.findByRole(
    'region',
    { name: `${text('scr01_admin.panel.title')} Cuenta sintética B` },
    WAIT,
  );
}

async function choose(user: User, container: HTMLElement, field: string, option: string) {
  await user.selectOptions(within(container).getByLabelText(text(field)), option);
}

describe('/admin/users', { timeout: 60_000 }, () => {
  it('lista las cuentas con estado, licencia y asignaciones', async () => {
    await mountAt('/admin/users', {
      users: [
        managedUser(),
        managedUser({
          user_id: '0190a1b2-0000-7000-8000-0000000000c3',
          display_name: 'Cuenta sintética C',
          status: 'invited',
          professional_license: 'LIC-SINT-0001',
          assignments: [
            {
              assignment_id: '0190a1b2-0000-7000-8000-0000000000d2',
              role: 'coordinator_sst',
              scope_level: 'plant',
              scope_id: PLANT,
            },
          ],
        }),
      ],
    });
    const table = await screen.findByRole('table', { name: text('scr01_admin.users.list') }, WAIT);
    await waitFor(() => {
      expect(within(table).getAllByRole('row')).toHaveLength(3);
    }, WAIT);
    const rows = within(table).getAllByRole('row');
    const first = nth(rows, 1);
    const second = nth(rows, 2);
    expect(within(first).getByText(label('account_status', 'active'))).toBeDefined();
    expect(within(first).getByText(text('scr01_admin.account.no_license'))).toBeDefined();
    expect(within(second).getByText(label('account_status', 'invited'))).toBeDefined();
    expect(within(second).getByText('LIC-SINT-0001')).toBeDefined();
    await waitFor(() => {
      expect(
        within(second).getByText(
          `${label('role', 'coordinator_sst')} · ${label('scope_level', 'plant')}: Planta sintética`,
        ),
      ).toBeDefined();
    }, WAIT);
  });

  it('sin usuarios muestra «aún no hay usuarios invitados»', async () => {
    await mountAt('/admin/users', { users: [] });
    expect(await screen.findByText(text('scr01_admin.users.empty'), {}, WAIT)).toBeDefined();
  });

  it('la invitación muestra el enlace una vez con su valor exacto y desaparece al salir', async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/users`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          {
            user_id: '0190a1b2-0000-7000-8000-0000000000c9',
            invitation_id: '0190a1b2-0000-7000-8000-0000000000e9',
            expires_at: '2026-01-08T12:00:00.000Z',
            delivery: 'link_disclosed',
            link: LINK,
          },
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    const fixture = await mountAt('/admin/users');
    await user.click(
      await screen.findByRole('button', { name: text('scr01_admin.invite.open') }, WAIT),
    );
    const form = screen.getByRole('form', { name: text('scr01_admin.invite.title') });
    await user.type(
      within(form).getByLabelText(text('scr01_admin.invite.email')),
      'nueva@ejemplo.test',
    );
    await user.type(within(form).getByLabelText(text('scr01_admin.invite.name')), 'Cuenta nueva');
    await choose(user, form, 'scr01_admin.assignment.role', 'coordinator_sst');
    await choose(user, form, 'scr01_admin.assignment.level', 'plant');
    await waitFor(() => {
      expect(within(form).getByRole('option', { name: 'Planta sintética' })).toBeDefined();
    }, WAIT);
    await choose(user, form, 'scr01_admin.assignment.scope', PLANT);
    await user.click(within(form).getByRole('button', { name: text('scr01_admin.invite.submit') }));

    const secret = await screen.findByRole(
      'region',
      { name: text('components_forms.secret.invitation_link') },
      WAIT,
    );
    expect(secret.querySelector('[data-part="value"]')?.textContent).toBe(LINK);
    expect(bodies).toEqual([
      {
        email: 'nueva@ejemplo.test',
        display_name: 'Cuenta nueva',
        assignments: [{ role: 'coordinator_sst', scope_level: 'plant', scope_id: PLANT }],
        disclose_link: true,
      },
    ]);
    // La caché de mutaciones solo guarda la forma de entrega, nunca el enlace.
    expect(
      fixture.cache.client
        .getMutationCache()
        .getAll()
        .map((mutation) => mutation.state.data),
    ).toEqual([{ delivery: 'link_disclosed' }]);

    await act(async () => {
      await fixture.router.navigate('/elsewhere');
    });
    await act(async () => {
      await fixture.router.navigate('/admin/users');
    });
    await screen.findByRole('table', { name: text('scr01_admin.users.list') }, WAIT);
    expect(
      screen.queryByRole('region', { name: text('components_forms.secret.invitation_link') }),
    ).toBeNull();
    expect(fixture.container.innerHTML.indexOf(LINK)).toBe(-1);
  });

  it('«lo guardé» retira el enlace de la pantalla', async () => {
    server.use(
      http.post(`${ORIGIN}/users`, () =>
        HttpResponse.json(
          {
            user_id: '0190a1b2-0000-7000-8000-0000000000c9',
            invitation_id: '0190a1b2-0000-7000-8000-0000000000e9',
            expires_at: '2026-01-08T12:00:00.000Z',
            delivery: 'link_disclosed',
            link: LINK,
          },
          { status: 201 },
        ),
      ),
    );
    const user = userEvent.setup();
    const fixture = await mountAt('/admin/users');
    await user.click(
      await screen.findByRole('button', { name: text('scr01_admin.invite.open') }, WAIT),
    );
    const form = screen.getByRole('form', { name: text('scr01_admin.invite.title') });
    await user.type(
      within(form).getByLabelText(text('scr01_admin.invite.email')),
      'nueva@ejemplo.test',
    );
    await user.type(within(form).getByLabelText(text('scr01_admin.invite.name')), 'Cuenta nueva');
    await choose(user, form, 'scr01_admin.assignment.role', 'administrator');
    await choose(user, form, 'scr01_admin.assignment.level', 'organization');
    await choose(user, form, 'scr01_admin.assignment.scope', ORGANIZATION);
    await user.click(within(form).getByRole('button', { name: text('scr01_admin.invite.submit') }));
    const secret = await screen.findByRole(
      'region',
      { name: text('components_forms.secret.invitation_link') },
      WAIT,
    );
    await user.click(
      within(secret).getByRole('button', { name: text('components_forms.secret.acknowledge') }),
    );
    expect(
      screen.queryByRole('region', { name: text('components_forms.secret.invitation_link') }),
    ).toBeNull();
    expect(fixture.container.innerHTML.indexOf(LINK)).toBe(-1);
  });

  it('una invitación incompleta no se envía', async () => {
    let posted = 0;
    server.use(
      http.post(`${ORIGIN}/users`, () => {
        posted += 1;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    const user = userEvent.setup();
    await mountAt('/admin/users');
    await user.click(
      await screen.findByRole('button', { name: text('scr01_admin.invite.open') }, WAIT),
    );
    const form = screen.getByRole('form', { name: text('scr01_admin.invite.title') });
    await user.type(within(form).getByLabelText(text('scr01_admin.invite.email')), 'sin-arroba');
    await user.click(within(form).getByRole('button', { name: text('scr01_admin.invite.submit') }));
    expect(
      within(form)
        .getAllByRole('alert')
        .map((alert) => alert.getAttribute('data-text-key')),
    ).toEqual([
      'scr01_admin.invite.email_invalid',
      'scr01_admin.invite.name_required',
      'scr01_admin.assignment.incomplete',
    ]);
    expect(posted).toBe(0);
  });

  it('el rol ofrece todos los valores, platform_operator incluido (BR-APP-43)', async () => {
    const user = userEvent.setup();
    await mountAt('/admin/users');
    await user.click(
      await screen.findByRole('button', { name: text('scr01_admin.invite.open') }, WAIT),
    );
    const form = screen.getByRole('form', { name: text('scr01_admin.invite.title') });
    const role = within(form).getByLabelText<HTMLSelectElement>(
      text('scr01_admin.assignment.role'),
    );
    // Oráculo literal del enum `Role` de U-02: la pantalla no recorta la lista.
    expect(
      Array.from(role.options)
        .map((option) => option.value)
        .filter((value) => value !== ''),
    ).toEqual([
      'coordinator_sst',
      'line_manager',
      'plant_manager',
      'administrator',
      'provider_installer',
      'copasst',
      'platform_operator',
    ]);
    expect(
      within(role).getByRole('option', { name: label('role', 'platform_operator') }),
    ).toBeDefined();
  });

  it('el conflict del último administrador al retirar el rol se ancla con el mensaje del servidor y conserva lo escrito', async () => {
    let removals = 0;
    server.use(
      http.delete(`${ORIGIN}/users/:user_id/roles/:assignment_id`, ({ params }) => {
        removals += 1;
        expect(params).toEqual({ user_id: USER_B, assignment_id: ADMIN_ASSIGNMENT });
        return apiError('conflict', 409, CONFLICT);
      }),
    );
    const user = userEvent.setup();
    await mountAt('/admin/users');
    const panel = await openPanel(user);
    const profile = within(panel).getByRole('form', { name: text('scr01_admin.profile.title') });
    const name = within(profile).getByLabelText<HTMLInputElement>(text('scr01_admin.profile.name'));
    await user.clear(name);
    await user.type(name, 'Nombre en edición');
    const roles = within(panel).getByRole('form', { name: text('scr01_admin.roles.title') });
    await choose(user, roles, 'scr01_admin.assignment.role', 'copasst');
    const caption = `${label('role', 'administrator')} · ${label('scope_level', 'organization')}: Organización sintética`;
    await waitFor(() => {
      expect(
        within(roles).getByRole('button', {
          name: `${text('scr01_admin.roles.remove')}: ${caption}`,
        }),
      ).toBeDefined();
    }, WAIT);
    await user.click(
      within(roles).getByRole('button', {
        name: `${text('scr01_admin.roles.remove')}: ${caption}`,
      }),
    );

    const notice = await within(roles).findByRole('alert', {}, WAIT);
    expect(notice.getAttribute('data-code')).toBe('conflict');
    expect(within(notice).getByText(CONFLICT)).toBeDefined();
    expect(roles.getAttribute('aria-describedby')).toBe(notice.id);
    expect(removals).toBe(1);
    // Lo escrito sigue: el nombre del perfil, el rol elegido y la asignación que no se retiró.
    expect(name.value).toBe('Nombre en edición');
    expect(
      within(roles).getByLabelText<HTMLSelectElement>(text('scr01_admin.assignment.role')).value,
    ).toBe('copasst');
    expect(within(roles).getByText(caption)).toBeDefined();
    expect(within(profile).queryByRole('alert')).toBeNull();
  });

  it('el conflict del último administrador al desactivar se ancla al formulario de estado', async () => {
    server.use(
      http.post(`${ORIGIN}/users/:user_id/deactivate`, () => apiError('conflict', 409, CONFLICT)),
    );
    const user = userEvent.setup();
    await mountAt('/admin/users');
    const panel = await openPanel(user);
    const profile = within(panel).getByRole('form', { name: text('scr01_admin.profile.title') });
    await user.type(within(profile).getByLabelText(text('scr01_admin.profile.license')), 'LIC-9');
    const status = within(panel).getByRole('form', { name: text('scr01_admin.status.title') });
    await user.click(
      within(status).getByRole('button', { name: text('scr01_admin.status.deactivate') }),
    );
    await user.click(
      within(status).getByRole('button', { name: text('scr01_admin.status.confirm') }),
    );
    const notice = await within(status).findByRole('alert', {}, WAIT);
    expect(notice.getAttribute('data-code')).toBe('conflict');
    expect(within(notice).getByText(CONFLICT)).toBeDefined();
    expect(within(status).getByText(label('account_status', 'active'))).toBeDefined();
    expect(
      within(profile).getByLabelText<HTMLInputElement>(text('scr01_admin.profile.license')).value,
    ).toBe('LIC-9');
  });

  it('un error que no es anchored no se ancla al formulario', async () => {
    server.use(
      http.post(`${ORIGIN}/users/:user_id/deactivate`, () =>
        apiError('internal_error', 500, platform.api_error_code.internal_error),
      ),
    );
    const user = userEvent.setup();
    await mountAt('/admin/users');
    const panel = await openPanel(user);
    const status = within(panel).getByRole('form', { name: text('scr01_admin.status.title') });
    await user.click(
      within(status).getByRole('button', { name: text('scr01_admin.status.deactivate') }),
    );
    await user.click(
      within(status).getByRole('button', { name: text('scr01_admin.status.confirm') }),
    );
    const alert = await within(status).findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-code')).toBe('internal_error');
    expect(alert.textContent).toBe(text('screen_state.error'));
    expect(status.getAttribute('aria-describedby')).toBeNull();
  });

  it('el restablecimiento del segundo factor exige motivo y después informa las sesiones cerradas', async () => {
    let resets = 0;
    server.use(
      http.post(`${ORIGIN}/users/:user_id/second-factor/reset`, ({ params }) => {
        resets += 1;
        return HttpResponse.json({ user_id: params.user_id, sessions_closed: 3 });
      }),
    );
    const user = userEvent.setup();
    await mountAt('/admin/users');
    const panel = await openPanel(user);
    const form = within(panel).getByRole('form', { name: text('scr01_admin.reset.title') });
    const submit = within(form).getByRole('button', { name: text('scr01_admin.reset.submit') });
    await user.click(submit);
    expect(resets).toBe(0);
    const reason = within(form).getByRole('textbox', { name: /./u });
    expect(reason.getAttribute('aria-invalid')).toBe('true');
    expect(within(form).getByText(text('validation.required'))).toBeDefined();
    await user.type(reason, 'corto');
    await user.click(submit);
    expect(resets).toBe(0);
    await user.clear(reason);
    await user.type(reason, 'Perdió el dispositivo de autenticación');
    await user.click(submit);
    const done = await within(form).findByRole('status', {}, WAIT);
    expect(done.getAttribute('data-text-key')).toBe('scr01_admin.reset.done');
    expect(within(done).getByText(`${text('scr01_admin.reset.done')} 3`)).toBeDefined();
    expect(within(done).getByText(text('scr01_admin.reset.enroll_next'))).toBeDefined();
    expect(resets).toBe(1);
  });

  it('axe: sin violaciones serias con la lista, la invitación y el panel abiertos', async () => {
    const user = userEvent.setup();
    const fixture = await mountAt('/admin/users');
    await user.click(
      await screen.findByRole('button', { name: text('scr01_admin.invite.open') }, WAIT),
    );
    await openPanel(user);
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
