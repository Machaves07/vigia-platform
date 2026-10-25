/**
 * `/provider/concessions` (SCR-02, proveedor; RV-04; IF-06; BR-APP-51, 67 a 69; A-8):
 *
 * - Lista sus concesiones activas, vencidas y revocadas; «operar bajo esta concesión» activa
 *   `ActiveConcession` y la petición siguiente (`GET /me`) ya lleva `X-Vigia-Concession`.
 * - La solicitud muestra la vista previa «el cliente verá» antes de enviar: sin ella no hay
 *   `POST`. El motivo de menos de 10 caracteres no llega al servidor.
 * - Un vencimiento por encima del tope del cliente vuelve con `conflict` anclado al campo de la
 *   vigencia y lo escrito se conserva.
 * - Sin concesión activa, el usuario del proveedor solo entra en este panel y en su cuenta.
 * - `axe` sin violaciones serias.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { canEnterGroup, homeScreen } from '@/app/navigation';
import { SCREENS } from '@/app/screens';
import { concessionsPort } from '@/ports/scr02Concessions';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import {
  apiError,
  CONCESSION,
  expectNoSerious,
  mountAt,
  nth,
  ORGANIZATION,
  ORIGIN,
  OTHER_CONCESSION,
  PLANT,
  providerConcession,
  providerMe,
  seriousViolations,
  WAIT,
} from './support';

type User = ReturnType<typeof userEvent.setup>;

const REASON = 'Ajuste de encuadre en la planta sintética';

function captureGrants(respond?: () => Response) {
  const bodies: unknown[] = [];
  server.use(
    http.post(`${ORIGIN}/provider/concessions`, async ({ request }) => {
      bodies.push(await request.json());
      return (
        respond?.() ??
        HttpResponse.json(providerConcession({ concession_id: OTHER_CONCESSION }), {
          status: 201,
        })
      );
    }),
  );
  return bodies;
}

async function openForm(user: User) {
  await user.click(await screen.findByRole('button', { name: text('scr02.request.open') }, WAIT));
  return screen.getByRole('form', { name: text('scr02.request.title') });
}

async function fillForm(user: User, form: HTMLElement, hours: string, reason = REASON) {
  await user.type(within(form).getByLabelText(text('scr02.request.client')), ORGANIZATION);
  await user.selectOptions(within(form).getByLabelText(text('scr02.request.level')), 'plant');
  await user.type(within(form).getByLabelText(text('scr02.request.plant')), PLANT);
  await user.type(within(form).getByRole('textbox', { name: /^Motivo/ }), reason);
  await user.type(within(form).getByLabelText(text('scr02.request.duration')), hours);
}

async function listTable() {
  const region = await screen.findByRole('region', { name: text('scr02.provider.list') }, WAIT);
  await waitFor(() => {
    expect(within(region).getAllByRole('row').length).toBeGreaterThan(1);
  }, WAIT);
  return region;
}

describe('/provider/concessions', { timeout: 60_000 }, () => {
  it('lista activas, vencidas y revocadas; «operar» solo en las activas', async () => {
    await mountAt('/provider/concessions', {
      providerConcessions: [
        providerConcession(),
        providerConcession({ concession_id: OTHER_CONCESSION, status: 'expired' }),
        providerConcession({
          concession_id: '0190a1b2-0000-7000-8000-0000000000c3',
          status: 'revoked',
          revoked_at: '2026-01-01T10:00:00.000Z',
          revoked_by_side: 'client',
        }),
      ],
    });
    const region = await listTable();
    const rows = within(region).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(3);
    expect(
      rows.map((row) =>
        within(row)
          .queryAllByRole('button')
          .map((button) => button.textContent),
      ),
    ).toEqual([[text('scr02.provider.operate')], [], []]);
    expect(within(nth(rows, 1)).getByText(label('concession_status', 'expired'))).toBeDefined();
    expect(within(nth(rows, 2)).getByText(text('scr02.revoked_by.client'))).toBeDefined();
    expect(within(nth(rows, 0)).getAllByText(ORGANIZATION)).toHaveLength(1);
  });

  it('«operar bajo esta concesión» activa la concesión y la cabecera sale en la petición siguiente', async () => {
    const user = userEvent.setup();
    const fixture = await mountAt('/provider/concessions');
    const region = await listTable();
    expect(fixture.meHeaders).toEqual([null]);
    await user.click(within(region).getByRole('button', { name: /^Operar bajo esta concesión/ }));
    expect(fixture.concession.getSnapshot().active?.concession_id).toBe(CONCESSION);
    // `GET /me` se vuelve a pedir bajo la concesión para reconstruir la navegación.
    await waitFor(() => {
      expect(fixture.meHeaders).toEqual([null, CONCESSION]);
    }, WAIT);
    expect(
      await within(region).findByText(text('scr02.provider.operating'), {}, WAIT),
    ).toBeDefined();
    // Cualquier petición del transporte real lleva ya la cabecera.
    await concessionsPort.hierarchy.read({});
    expect(fixture.hierarchyHeaders).toEqual([CONCESSION]);
  });

  it('sin «operar», ninguna petición lleva la cabecera de concesión', async () => {
    const fixture = await mountAt('/provider/concessions');
    await listTable();
    await concessionsPort.hierarchy.read({});
    expect(fixture.hierarchyHeaders).toEqual([null]);
    expect(fixture.concession.getSnapshot().active).toBeNull();
  });

  it('con otra concesión ya activa, «operar» no la cambia y lo dice', async () => {
    const user = userEvent.setup();
    const fixture = await mountAt('/provider/concessions', {
      providerConcessions: [
        providerConcession(),
        providerConcession({ concession_id: OTHER_CONCESSION }),
      ],
    });
    const region = await listTable();
    const buttons = within(region).getAllByRole('button', { name: /^Operar bajo esta concesión/ });
    await user.click(nth(buttons, 0));
    await user.click(
      nth(within(region).getAllByRole('button', { name: /^Operar bajo esta concesión/ }), 0),
    );
    expect(fixture.concession.getSnapshot().active?.concession_id).toBe(CONCESSION);
    expect((await screen.findByRole('alert', {}, WAIT)).getAttribute('data-text-key')).toBe(
      'scr02.provider.operate_refused',
    );
  });

  it('la vista previa «el cliente verá» se muestra antes de enviar y solo desde ella se envía', async () => {
    const bodies = captureGrants();
    const user = userEvent.setup();
    const fixture = await mountAt('/provider/concessions');
    await listTable();
    const form = await openForm(user);
    await fillForm(user, form, '48');
    await user.click(within(form).getByRole('button', { name: text('scr02.request.review') }));

    const preview = await screen.findByRole('region', { name: text('scr02.preview.title') }, WAIT);
    expect(bodies).toEqual([]);
    const terms = within(preview)
      .getAllByRole('term')
      .map((term) => term.textContent);
    expect(terms).toEqual([
      text('scr02.preview.who'),
      text('scr02.preview.scope'),
      text('scr02.preview.reason'),
      text('scr02.preview.validity'),
      text('scr02.preview.queries'),
    ]);
    const values = within(preview)
      .getAllByRole('definition')
      .map((value) => value.textContent);
    expect(values[0]).toBe('Persona sintética · Proveedora sintética');
    expect(values[1]).toBe(`${label('scope_level', 'plant')}: ${PLANT}`);
    expect(values[2]).toBe(REASON);
    expect(values[3]).toBe(text('scr02.preview.validity_value').replace('{hours}', '48'));
    expect(values[4]).toBe(text('scr02.preview.queries_value'));

    const listed = fixture.requests.providerConcessions;
    await user.click(within(preview).getByRole('button', { name: text('scr02.preview.submit') }));
    expect(await screen.findByText(text('scr02.request.done'), {}, WAIT)).toBeDefined();
    expect(bodies).toEqual([
      {
        client_organization_id: ORGANIZATION,
        scope_level: 'plant',
        scope_id: PLANT,
        reason: REASON,
        duration_hours: 48,
      },
    ]);
    await waitFor(() => {
      expect(fixture.requests.providerConcessions).toBe(listed + 1);
    }, WAIT);
  });

  it('con alcance de organización, el alcance es la propia organización cliente', async () => {
    const bodies = captureGrants();
    const user = userEvent.setup();
    await mountAt('/provider/concessions');
    await listTable();
    const form = await openForm(user);
    await user.type(within(form).getByLabelText(text('scr02.request.client')), ORGANIZATION);
    await user.type(within(form).getByRole('textbox', { name: /^Motivo/ }), REASON);
    await user.type(within(form).getByLabelText(text('scr02.request.duration')), '1');
    await user.click(within(form).getByRole('button', { name: text('scr02.request.review') }));
    const preview = await screen.findByRole('region', { name: text('scr02.preview.title') }, WAIT);
    await user.click(within(preview).getByRole('button', { name: text('scr02.preview.submit') }));
    await screen.findByText(text('scr02.request.done'), {}, WAIT);
    expect(bodies).toEqual([
      {
        client_organization_id: ORGANIZATION,
        scope_level: 'organization',
        scope_id: ORGANIZATION,
        reason: REASON,
        duration_hours: 1,
      },
    ]);
  });

  it('un vencimiento por encima del tope vuelve con conflict anclado a la vigencia y conserva lo escrito', async () => {
    const bodies = captureGrants(() =>
      apiError('conflict', 409, 'La vigencia supera el tope del cliente.'),
    );
    const user = userEvent.setup();
    await mountAt('/provider/concessions');
    await listTable();
    const form = await openForm(user);
    await fillForm(user, form, '720');
    await user.click(within(form).getByRole('button', { name: text('scr02.request.review') }));
    const preview = await screen.findByRole('region', { name: text('scr02.preview.title') }, WAIT);
    await user.click(within(preview).getByRole('button', { name: text('scr02.preview.submit') }));

    const again = await screen.findByRole('form', { name: text('scr02.request.title') }, WAIT);
    expect(bodies).toHaveLength(1);
    const duration = within(again).getByLabelText<HTMLInputElement>(text('scr02.request.duration'));
    const alert = await within(again).findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-code')).toBe('conflict');
    expect(duration.getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
    expect(duration.getAttribute('aria-invalid')).toBe('true');
    expect(again.getAttribute('aria-describedby')).toBeNull();
    expect(duration.value).toBe('720');
    expect(within(again).getByLabelText<HTMLInputElement>(text('scr02.request.client')).value).toBe(
      ORGANIZATION,
    );
    expect(within(again).getByLabelText<HTMLInputElement>(text('scr02.request.plant')).value).toBe(
      PLANT,
    );
    expect(within(again).getByRole<HTMLTextAreaElement>('textbox', { name: /^Motivo/ }).value).toBe(
      REASON,
    );
  });

  it('un motivo de menos de 10 caracteres o una vigencia fuera de rango no llegan a la vista previa', async () => {
    const bodies = captureGrants();
    const user = userEvent.setup();
    await mountAt('/provider/concessions');
    await listTable();
    const form = await openForm(user);
    await fillForm(user, form, '0', 'Corto');
    await user.click(within(form).getByRole('button', { name: text('scr02.request.review') }));
    expect(screen.queryByRole('region', { name: text('scr02.preview.title') })).toBeNull();
    const keys = within(form)
      .getAllByRole('alert')
      .map((node) => node.getAttribute('data-text-key'));
    expect(keys).toContain('scr02.request.invalid_duration');
    expect(form.querySelector('[data-part="local_error"]')).not.toBeNull();
    expect(bodies).toEqual([]);
  });

  it('sin concesión activa, el usuario del proveedor solo entra en este panel y en su cuenta', () => {
    const me = providerMe(new FakeClock(0));
    const permissions = new Set(me.effective_permissions);
    const groups = SCREENS.flatMap((screenDescriptor) =>
      screenDescriptor.route_groups
        .filter((group) => group.access !== 'public' && canEnterGroup(group, permissions))
        .map((group) => [screenDescriptor.screen_id, group.id]),
    );
    expect(groups).toEqual([
      ['SCR-01', 'account'],
      ['SCR-02', 'provider'],
    ]);
    expect(homeScreen(me.assignments, permissions)).toMatchObject({
      screen_id: 'SCR-02',
      group: 'provider',
      path: '/provider/concessions',
    });
  });

  it('axe sin violaciones serias en el panel, el formulario y la vista previa', async () => {
    captureGrants();
    const user = userEvent.setup();
    const fixture = await mountAt('/provider/concessions');
    await listTable();
    expectNoSerious(await seriousViolations(fixture.container));
    const form = await openForm(user);
    await fillForm(user, form, '24');
    expectNoSerious(await seriousViolations(fixture.container));
    await user.click(within(form).getByRole('button', { name: text('scr02.request.review') }));
    await screen.findByRole('region', { name: text('scr02.preview.title') }, WAIT);
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
