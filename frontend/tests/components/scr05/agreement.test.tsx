/**
 * SCR-05 · acuerdo de uso (IF-11 paso 5; BR-APP-52, 55):
 *
 * - «Confirmar mi firma» solo aparece cuando el servidor responde `pending_confirmation_for_me`
 *   para la persona autenticada (y con `agreements.sign`); envía ese `agreement_id` **sin
 *   cuerpo**: MSW rechaza cualquier cuerpo, así que un `user_id` elegido por la pantalla cae.
 * - El alta envía `signatories[{role, user_id}]`, documento opcional y `replaces_agreement_id`, y
 *   ancla los rechazos de §4.
 * - Tras aprobar, se invalidan las compuertas de SCR-05 y del panel de inicio y el chip pasa a
 *   «productiva» con la respuesta simulada; `catalog_plant_policy_missing` se ancla aquí.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Gates from '@/pages/scr05/gates';
import { screenKey } from '@/state/query';
import { text } from '@/texts';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  agreement,
  AGREEMENT,
  chips,
  gateState,
  gatesFixture,
  INSTALLER,
  INSTANT,
  ORIGIN,
  part,
  OTHER_USER,
  PENDING,
  plantPolicy,
  SELF,
  serveReads,
  THIRD_USER,
  transparency,
  typeInto,
  WAIT,
  ZONE,
  type Mode,
} from './support';

const ZONE_PATH = `/zones/${ZONE}/gates`;
const confirmButton = () => screen.queryByRole('button', { name: text('scr05.confirm.action') });

interface Sent {
  readonly agreementId: string;
  readonly body: string;
}

/** La confirmación: cualquier cuerpo es un intento de firmar por otra persona y se rechaza. */
function serveConfirmations(onConfirmed: () => void = () => undefined) {
  const sent: Sent[] = [];
  server.use(
    http.post(
      `${ORIGIN}/use-agreements/:agreement_id/confirmations`,
      async ({ request, params }) => {
        const body = await request.text();
        const agreementId = String(params.agreement_id);
        sent.push({ agreementId, body });
        if (body !== '') return apiError('invalid_request', 422);
        onConfirmed();
        return HttpResponse.json(
          {
            agreement_id: agreementId,
            user_id: SELF,
            role_in_use: 'provider_installer',
            confirmed_at: INSTANT,
            origin: 'management',
          },
          { status: 201 },
        );
      },
    ),
  );
  return sent;
}

async function renderZone(permissions: readonly string[] = INSTALLER) {
  const fixture = await gatesFixture(ZONE_PATH, permissions);
  const view = render(<Gates />, { wrapper: fixture.Wrapper });
  await screen.findByRole('heading', { name: text('scr05.agreement.title') }, WAIT);
  return { ...fixture, ...view };
}

describe('SCR-05 confirmar mi firma (BR-APP-52)', { timeout: 40_000 }, () => {
  it('sin pending_confirmation_for_me no se ofrece la acción', async () => {
    serveReads({ transparency: () => transparency(null) });
    const sent = serveConfirmations();
    const { container } = await renderZone();
    await waitFor(() => {
      expect(container.querySelector('[data-part="agreement_form"]')).not.toBeNull();
    }, WAIT);
    expect(confirmButton()).toBeNull();
    expect(container.querySelector('[data-part="confirm"]')).toBeNull();
    expect(sent).toEqual([]);
  });

  it('sin agreements.sign tampoco se ofrece, aunque el servidor pida la firma (el COPASST firma en SCR-16)', async () => {
    serveReads({ transparency: () => transparency(PENDING) });
    const sent = serveConfirmations();
    await renderZone(['catalog.read', 'transparency.read']);
    await screen.findByText(text('scr05.agreement.none'), {}, WAIT);
    expect(confirmButton()).toBeNull();
    expect(sent).toEqual([]);
  });

  it('con la firma pendiente envía ese agreement_id sin cuerpo ni usuario y muestra el acuse', async () => {
    let pending: string | null = PENDING;
    serveReads({ transparency: () => transparency(pending) });
    const sent = serveConfirmations(() => {
      pending = null;
    });
    const { container } = await renderZone();
    const button = await screen.findByRole('button', { name: text('scr05.confirm.action') }, WAIT);
    const summary = container.querySelector('[data-part="confirm"]');
    expect(summary?.querySelector('h2')?.textContent).toBe(text('scr05.confirm.title'));
    expect(
      within(summary as HTMLElement).getByText(text('scr05.confirm.own_session')),
    ).toBeDefined();
    // Ningún campo de la confirmación permite elegir a otra persona.
    expect(within(summary as HTMLElement).queryAllByRole('textbox')).toEqual([]);
    expect(within(summary as HTMLElement).queryAllByRole('combobox')).toEqual([]);
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(button);
    await waitFor(() => {
      expect(sent).toHaveLength(1);
    }, WAIT);
    expect(sent).toEqual([{ agreementId: PENDING, body: '' }]);
    const confirmed = await screen.findByRole('region', { name: text('scr05.confirm.done') }, WAIT);
    expect(confirmed.querySelector('[role="status"]')?.getAttribute('data-role-in-use')).toBe(
      'provider_installer',
    );
    // El refresco tras la escritura ya no trae la firma pendiente: la acción desaparece.
    await waitFor(() => {
      expect(confirmButton()).toBeNull();
    }, WAIT);
    expect(container.querySelector('[data-part="confirmed"]')).not.toBeNull();
    expect(sent).toHaveLength(1);
  });

  it('el rechazo catalog_signatory_not_expected se ancla con su código', async () => {
    serveReads({ transparency: () => transparency(PENDING) });
    server.use(
      http.post(`${ORIGIN}/use-agreements/:agreement_id/confirmations`, () =>
        apiError('conflict', 409, { detailCode: 'catalog_signatory_not_expected' }),
      ),
    );
    const { container } = await renderZone();
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr05.confirm.action') }, WAIT),
    );
    const confirm = part(container, 'confirm');
    const alert = await within(confirm).findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('conflict');
    expect(container.querySelector('[data-part="confirmed"]')).toBeNull();
  });
});

describe('SCR-05 alta del acuerdo de uso', { timeout: 40_000 }, () => {
  it('envía firmantes con rol y usuario, sin documento ni sustitución, y muestra el acuerdo', async () => {
    serveReads();
    const bodies: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/zones/:zone_id/use-agreements`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(agreement(), { status: 201 });
      }),
    );
    const { container } = await renderZone();
    await waitFor(() => {
      expect(container.querySelectorAll('[data-part="signatory_row"]')).toHaveLength(3);
    }, WAIT);
    // Un identificador mal formado no sale a la red.
    fireEvent.click(screen.getByRole('button', { name: text('scr05.agreement.create') }));
    await screen.findAllByText(text('scr05.agreement.user_id_invalid'), {}, WAIT);
    expect(bodies).toEqual([]);
    typeInto(`${text('scr05.agreement.user_id')} 1`, SELF);
    typeInto(`${text('scr05.agreement.user_id')} 2`, OTHER_USER);
    typeInto(`${text('scr05.agreement.user_id')} 3`, THIRD_USER);
    fireEvent.click(screen.getByRole('button', { name: text('scr05.agreement.create') }));
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect(bodies[0]).toEqual({
      signatories: [
        { role: 'plant_manager', user_id: SELF },
        { role: 'coordinator_sst', user_id: OTHER_USER },
        { role: 'copasst', user_id: THIRD_USER },
      ],
      document_ref: null,
      replaces_agreement_id: null,
    });
    const created = await waitFor(() => {
      const node = container.querySelector('[data-part="created_agreement"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    }, WAIT);
    expect(created.querySelector('[data-part="agreement_status"]')?.textContent).not.toBe('');
    expect(within(created).getAllByText(text('scr05.agreement.pending'))).toHaveLength(3);
  });

  it('con acuerdo vigente aprobado puede sustituirlo (replaces_agreement_id)', async () => {
    serveReads({ gates: () => gateState('productive') });
    const bodies: unknown[] = [];
    server.use(
      http.post(`${ORIGIN}/zones/:zone_id/use-agreements`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(agreement({ replaces_agreement_id: AGREEMENT }), { status: 201 });
      }),
    );
    await renderZone();
    const replaces = await screen.findByRole(
      'checkbox',
      { name: text('scr05.agreement.replaces') },
      WAIT,
    );
    typeInto(`${text('scr05.agreement.user_id')} 1`, SELF);
    typeInto(`${text('scr05.agreement.user_id')} 2`, OTHER_USER);
    typeInto(`${text('scr05.agreement.user_id')} 3`, THIRD_USER);
    fireEvent.click(replaces);
    fireEvent.click(screen.getByRole('button', { name: text('scr05.agreement.create') }));
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect((bodies[0] as { replaces_agreement_id: unknown }).replaces_agreement_id).toBe(AGREEMENT);
  });

  it.each([
    'catalog_fewer_than_three',
    'catalog_workers_representation_missing',
    'catalog_signatory_role_not_in_policy',
    'catalog_signatory_user_role_mismatch',
    'catalog_agreement_reused_from_other_zone',
  ])('el rechazo %s se ancla al formulario del acuerdo', async (detailCode) => {
    serveReads();
    server.use(
      http.post(`${ORIGIN}/zones/:zone_id/use-agreements`, () =>
        apiError('conflict', 409, { detailCode }),
      ),
    );
    const { container } = await renderZone();
    await screen.findByRole('button', { name: text('scr05.agreement.create') }, WAIT);
    typeInto(`${text('scr05.agreement.user_id')} 1`, SELF);
    typeInto(`${text('scr05.agreement.user_id')} 2`, OTHER_USER);
    typeInto(`${text('scr05.agreement.user_id')} 3`, THIRD_USER);
    fireEvent.click(screen.getByRole('button', { name: text('scr05.agreement.create') }));
    const form = part(container, 'agreement_form');
    const alert = await within(form).findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('conflict');
    expect(container.querySelector('[data-part="created_agreement"]')).toBeNull();
  });
});

describe('SCR-05 aprobación del acuerdo', { timeout: 40_000 }, () => {
  it('tras aprobar invalida compuertas de SCR-05 y del panel y el chip pasa a productiva', async () => {
    let mode: Mode = 'commissioning';
    const reads = serveReads({
      gates: () => gateState(mode),
      transparency: () => transparency(PENDING, mode),
    });
    const approvals: string[] = [];
    server.use(
      http.post(`${ORIGIN}/use-agreements/:agreement_id/approval`, ({ params }) => {
        approvals.push(String(params.agreement_id));
        mode = 'productive';
        return HttpResponse.json({
          agreement: agreement({ status: 'approved', approved_at: INSTANT, approved_by: SELF }),
          gates: gateState('productive'),
        });
      }),
    );
    const { cache, container } = await renderZone();
    await waitFor(() => {
      expect(chips(container)).toEqual(['no_use_agreement']);
    }, WAIT);
    // Entrada del panel de inicio con las compuertas de la zona.
    const homeKey = screenKey({
      screen: 'home',
      operation: 'GET /zones/{zone_id}/gates',
      scope: 'all',
      concession: null,
      params: { zone_id: ZONE },
    });
    cache.client.setQueryData(homeKey, gateState('commissioning'));
    expect(cache.client.getQueryState(homeKey)?.isInvalidated).toBe(false);
    const before = reads.filter((read) => read === `gates:${ZONE}`).length;
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr05.approval.action') }, WAIT),
    );
    await waitFor(() => {
      expect(chips(container)).toEqual(['productive']);
    }, WAIT);
    expect(approvals).toEqual([PENDING]);
    expect(cache.client.getQueryState(homeKey)?.isInvalidated).toBe(true);
    expect(reads.filter((read) => read === `gates:${ZONE}`).length).toBeGreaterThan(before);
    expect(container.querySelector('[data-part="resulting_mode"]')?.textContent).not.toBe('');
    await screen.findByText(text('scr05.approval.done'), {}, WAIT);
  });

  it.each([
    'catalog_plant_policy_missing',
    'catalog_signatures_incomplete',
    'catalog_commissioning_record_missing',
    'catalog_mounting_gate_pending',
  ])('el rechazo %s se ancla a la aprobación', async (detailCode) => {
    serveReads({ policy: plantPolicy(false), transparency: () => transparency(PENDING) });
    server.use(
      http.post(`${ORIGIN}/use-agreements/:agreement_id/approval`, () =>
        apiError('conflict', 409, { detailCode }),
      ),
    );
    const { container } = await renderZone();
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr05.approval.action') }, WAIT),
    );
    const approval = part(container, 'approval');
    const alert = await within(approval).findByRole('alert', {}, WAIT);
    expect(alert.dataset.code).toBe('conflict');
    expect(chips(container)).toEqual(['no_use_agreement']);
  });
});
