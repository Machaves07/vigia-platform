import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { closurePort, readHistory, type Detail } from '@/ports/scr09Closure';
import { text } from '@/texts';
import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  ASSIGNEE,
  ATTACHMENT,
  DESCRIPTION,
  FINDING,
  INSTANT,
  LICENSE,
  PERMISSIONS,
  WAIT,
  detail,
  ledgerRecord,
  load,
  reasonName,
  renderClosure,
} from './closure_support';

describe('SCR-09 cierre y navegación de adjuntos', { timeout: 40_000 }, () => {
  it('la falta de licencia no oculta la firma y el rechazo del servidor muestra la guía anclada', async () => {
    const { container } = await renderClosure('closure', { license: null });
    const user = await load();
    const bodies: unknown[] = [];
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/closure`, async ({ request }) => {
        bodies.push(await request.json());
        return apiError('invalid_request', 400, { detailCode: 'loop_signer_license_missing' });
      }),
    );
    await user.type(
      screen.getByRole('textbox', { name: reasonName('scr09.closure.sign_description') }),
      DESCRIPTION,
    );
    await user.click(screen.getByRole('button', { name: text('components_forms.signature.sign') }));
    await screen.findByText(text('components_forms.signature.guide_license_missing'), {}, WAIT);
    const signature = screen.getByRole('region', {
      name: text('components_forms.signature.title'),
    });
    expect(signature.querySelector('[data-code="invalid_request"]')).not.toBeNull();
    expect(bodies).toEqual([
      { closure_description: DESCRIPTION, verification_note: null, professional_license: null },
    ]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it.each([
    ['loop_closure_evidence_missing', 'scr09.closure.evidence_guide'],
    ['loop_closure_requires_assigned_action', 'scr09.closure.action_guide'],
  ])('ancla %s con su guía sin ocultar la acción', async (code, guide) => {
    await renderClosure();
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/closure`, () =>
        apiError(
          code === 'loop_closure_evidence_missing' ? 'invalid_request' : 'conflict',
          code === 'loop_closure_evidence_missing' ? 400 : 409,
          { detailCode: code },
        ),
      ),
    );
    await user.type(
      screen.getByRole('textbox', { name: reasonName('scr09.closure.sign_description') }),
      DESCRIPTION,
    );
    await user.click(screen.getByRole('button', { name: text('components_forms.signature.sign') }));
    await screen.findByText(text(guide), {}, WAIT);
    expect(
      screen.getByRole('button', { name: text('components_forms.signature.sign') }),
    ).not.toBeNull();
  });

  it('firma con la licencia del perfil y registra un cierre nuevo', async () => {
    await renderClosure();
    const user = await load();
    const bodies: unknown[] = [];
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/closure`, async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          { ...(exampleFor('close_loop_findings__finding_state_id__closure_post') as object) },
          { status: 201 },
        );
      }),
    );
    await user.type(
      screen.getByRole('textbox', { name: reasonName('scr09.closure.sign_description') }),
      DESCRIPTION,
    );
    await user.click(screen.getByRole('button', { name: text('components_forms.signature.sign') }));
    await screen.findByText(text('scr09.closure.saved'), {}, WAIT);
    expect(bodies).toEqual([
      { closure_description: DESCRIPTION, verification_note: null, professional_license: LICENSE },
    ]);
  });

  it('la URL del adjunto solo aparece tras pedirla y se abre por navegación, nunca incrustada', async () => {
    let reads = 0;
    await renderClosure('closure', {
      records: [
        ledgerRecord('action_evidence_added', { attachments: [{ attachment_id: ATTACHMENT }] }),
      ],
    });
    const user = await load();
    server.use(
      http.post(`http://localhost/loop/attachments/${ATTACHMENT}/read-url`, () => {
        reads++;
        return HttpResponse.json({
          attachment_id: ATTACHMENT,
          url: 'https://example.invalid/synthetic.pdf',
          expires_at: '2026-10-25T02:15:00.000Z',
          content_disposition: 'attachment',
        });
      }),
    );
    expect(reads).toBe(0);
    await user.click(screen.getByRole('button', { name: text('media.attachment.prepare') }));
    const link = await screen.findByRole('link', { name: text('media.attachment.open') }, WAIT);
    expect(link.getAttribute('href')).toBe('https://example.invalid/synthetic.pdf');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(document.querySelector('img,iframe,object,embed')).toBeNull();
    expect(reads).toBe(1);
  });

  it('la consulta auditada espera el clic y separa organización, planta, zona y sujeto incluso entre páginas', async () => {
    const rendered = await renderClosure();
    let pages = 0;
    const record = { ...ledgerRecord(), organization_id: rendered.organizationId };
    server.use(
      http.get('http://localhost/ledger/records', ({ request }) => {
        pages++;
        const query = new URL(request.url).searchParams;
        expect(query.get('plant_id')).toBe(detail.plant_id);
        expect(query.get('zone_id')).toBe(detail.zone_id);
        return HttpResponse.json({
          items:
            pages === 1
              ? [
                  {
                    ...record,
                    content: { subject_id: FINDING, closure_description: 'PLANTA-INCORRECTA' },
                    plant_id: FINDING,
                  },
                  {
                    ...record,
                    content: { subject_id: FINDING, closure_description: 'ZONA-INCORRECTA' },
                    scope: { ...record.scope, zone_id: FINDING },
                  },
                  {
                    ...record,
                    content: {
                      subject_id: FINDING,
                      closure_description: 'ORGANIZACION-INCORRECTA',
                    },
                    organization_id: FINDING,
                  },
                  {
                    ...record,
                    content: {
                      subject_id: detail.plant_id,
                      closure_description: 'SUJETO-INCORRECTO',
                    },
                  },
                ]
              : [record],
          next_cursor: pages === 1 ? { received_at: INSTANT, record_id: FINDING } : null,
        });
      }),
    );
    expect(pages).toBe(0);
    await load();
    await waitFor(() => {
      expect(pages).toBe(2);
    }, WAIT);
    const history = screen.getByRole('region', { name: text('scr09.closure.history') });
    expect(await within(history).findByText(DESCRIPTION, {}, WAIT)).not.toBeNull();
    for (const value of [
      'PLANTA-INCORRECTA',
      'ZONA-INCORRECTA',
      'ORGANIZACION-INCORRECTA',
      'SUJETO-INCORRECTO',
    ])
      expect(within(history).queryByText(value)).toBeNull();
    pages = 0;
    expect(
      await readHistory(detail, rendered.organizationId, new AbortController().signal),
    ).toEqual([record]);
  });

  it('vencidos del coordinador siempre visibles y resumen del gerente por su permiso sin filtros', async () => {
    await renderClosure('closure', { permissions: [...PERMISSIONS, 'executive_summary.read'] });
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/loop/overdue', ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({
          plant_id: detail.plant_id,
          highlight_threshold_days: 30,
          items: [],
          zones: [{ zone_id: detail.zone_id, overdue_count: 7, highlighted_count: 3 }],
          partition_notice: null,
          next_cursor: null,
        });
      }),
    );
    await load();
    const overdue = screen.getByRole('region', { name: text('scr09.overdue.title') });
    await within(overdue).findByText(
      `${text('scr09.overdue.zone')}: Zona sintética · ${text('scr09.overdue.count')}: 7 · ${text('scr09.overdue.highlighted')}: 3`,
      {},
      WAIT,
    );
    expect(within(overdue).getByText(`${text('scr09.overdue.count')}: 1`)).not.toBeNull();
    expect([...(requests[0]?.searchParams.keys() ?? [])].sort()).toEqual(['limit', 'plant_id']);
    expect(closurePort.operations.every((operation) => operation.status === 'fixed')).toBe(true);
  });

  /**
   * O-16 (recorrido de cierre M10): cada fila de «Acciones vencidas» nombra al responsable de la
   * acción por su nombre visible, nunca por su identificador; sin nombre, «Responsable no
   * disponible». Mutaciones con nombre: `m_sin_responsable_vencidas` y
   * `m_responsable_id_en_vez_de_nombre`.
   */
  describe('O-16: el responsable de cada vencida', () => {
    const OTHER_USER = '0190a1b2-4444-7000-8000-000000000002';
    function item(index: number, name: string | null) {
      return {
        finding_state_id: FINDING,
        plant_id: detail.plant_id,
        zone_id: detail.zone_id,
        subject_kind: 'finding',
        family: 'coexistence',
        tier: 'tier_1',
        lifecycle_state: 'overdue',
        declared: true,
        action_id: `0190a1b2-7777-7000-8000-00000000010${String(index)}`,
        action_type: 'engineering',
        assignee_user_id: OTHER_USER,
        assignee_inactive: false,
        assignee_display_name: name,
        due_at: '2026-10-01T15:00:00.000Z',
        overdue_since: '2026-10-01T15:00:00.000Z',
        overdue_days: 23 + index,
        highlighted: false,
      };
    }
    const named = (name: string) => `${text('finding_journey.assignee')}: ${name}`;

    it('el panel del gerente y la vencida del lazo nombran a su responsable', async () => {
      await renderClosure('closure', { permissions: [...PERMISSIONS, 'executive_summary.read'] });
      server.use(
        http.get('http://localhost/loop/overdue', () =>
          HttpResponse.json({
            plant_id: detail.plant_id,
            highlight_threshold_days: 30,
            items: [item(1, 'Persona de prueba (plant_manager)'), item(2, null)],
            zones: [{ zone_id: detail.zone_id, overdue_count: 2, highlighted_count: 0 }],
            partition_notice: null,
            next_cursor: null,
          }),
        ),
      );
      await load();
      const region = screen.getByRole('region', { name: text('scr09.overdue.title') });
      await waitFor(() => {
        expect(region.querySelectorAll('li > [data-part="overdue_assignee"]')).toHaveLength(2);
      }, WAIT);
      expect(
        [...region.querySelectorAll('li > [data-part="overdue_assignee"]')].map(
          (node) => node.textContent,
        ),
      ).toEqual([
        named('Persona de prueba (plant_manager)'),
        text('finding_journey.assignee_unavailable'),
      ]);
      expect(
        [
          ...region.querySelectorAll('[data-part="overdue_action"] [data-part="overdue_assignee"]'),
        ].map((node) => node.textContent),
      ).toEqual([named(ASSIGNEE)]);
    });

    it('la acción vigente con su plazo nombra también a su responsable', async () => {
      const [action] = detail.actions;
      if (action === undefined) throw new Error('Falta la acción sintética.');
      await renderClosure('closure', {
        detail: {
          ...detail,
          lifecycle_state: 'action_assigned',
          actions: [{ ...action, state: 'assigned', due_at: '2027-01-02T15:00:00.000Z' }],
        },
      });
      await load();
      const region = screen.getByRole('region', { name: text('scr09.overdue.title') });
      expect(
        [...region.querySelectorAll('[data-part="live_assignee"]')].map((node) => node.textContent),
      ).toEqual([named(ASSIGNEE)]);
    });
  });

  /**
   * O-4 (TASK-582): «Acciones vencidas» cuenta lo que el servidor declaró vencido
   * (`state = overdue`, BR-LAZ-46) y que cuenta para los vencimientos (`counts_as_overdue`,
   * BR-LAZ-24). El reloj inyectado (START, 25-10-2026) y el del navegador quedan después de los
   * plazos pasados y antes de los de 2027: ninguno de los dos decide.
   */
  describe('O-4: acciones vencidas sin cifras que engañan', () => {
    type Action = Detail['actions'][number];
    function withAction(change: Partial<Action>): Detail {
      const [action] = detail.actions;
      if (action === undefined) throw new Error('Falta la acción sintética.');
      return { ...detail, actions: [{ ...action, ...change }] };
    }
    async function overdueOf(change: Partial<Action>) {
      await renderClosure('closure', { detail: withAction(change) });
      await load();
      const region = screen.getByRole('region', { name: text('scr09.overdue.title') });
      return {
        count: region.querySelector('[data-part="overdue_count"]')?.textContent,
        overdue: [...region.querySelectorAll('[data-part="overdue_action"] time')].map((time) =>
          time.getAttribute('datetime'),
        ),
        live: [...region.querySelectorAll('[data-part="live_action"]')].map((entry) => ({
          label: entry.querySelector('[data-part="due_on"]')?.textContent,
          date: entry.querySelector('time')?.textContent,
        })),
      };
    }
    const count = (value: number) => `${text('scr09.overdue.count')}: ${String(value)}`;

    it('una acción vigente que vence en 2027 no es vencida: se muestra con su plazo', async () => {
      expect(
        await overdueOf({
          state: 'assigned',
          counts_as_overdue: true,
          due_at: '2027-01-02T15:00:00.000Z',
        }),
      ).toEqual({
        count: count(0),
        overdue: [],
        live: [{ label: text('scr09.overdue.due_on'), date: '02/01/2027' }],
      });
    });

    it.each([
      ['cuenta para los vencimientos', true, 1],
      ['con el lazo suspendido por reclasificación (BR-LAZ-24)', false, 0],
    ])('una acción overdue que %s da el contador del servidor', async (_, counts, expected) => {
      const result = await overdueOf({
        state: 'overdue',
        counts_as_overdue: counts,
        due_at: '2026-10-01T15:00:00.000Z',
      });
      expect(result.count).toBe(count(expected));
      expect(result.overdue).toHaveLength(expected);
      expect(result.live).toEqual([]);
    });

    /**
     * TASK-586 (menores 1 y 2 de la revisión de TASK-582): el cierre firmado no reescribe la fila
     * de la acción (VIG-233). Con el hallazgo cerrado, una acción que quedó `overdue` ya no cuenta
     * como vencida y una `assigned` no anuncia un plazo; con el lazo suspendido, tampoco.
     */
    it.each([
      ['closed', 'overdue', true],
      ['closed', 'assigned', true],
      ['suspended_by_reclassification', 'assigned', true],
      ['suspended_by_reclassification', 'overdue', false],
    ] as const)(
      'con el hallazgo %s, una acción %s no cuenta ni anuncia plazo',
      async (lifecycle, state, counts) => {
        const [action] = detail.actions;
        if (action === undefined) throw new Error('Falta la acción sintética.');
        await renderClosure('closure', {
          detail: {
            ...detail,
            lifecycle_state: lifecycle,
            actions: [
              { ...action, state, counts_as_overdue: counts, due_at: '2026-10-01T15:00:00.000Z' },
            ],
          },
        });
        await load();
        const region = screen.getByRole('region', { name: text('scr09.overdue.title') });
        expect(region.querySelector('[data-part="overdue_count"]')?.textContent).toBe(count(0));
        expect(region.querySelectorAll('[data-part="overdue_action"]')).toHaveLength(0);
        expect(region.querySelectorAll('[data-part="live_action"]')).toHaveLength(0);
      },
    );

    it('un solo reloj: con el plazo pasado y la acción aún assigned, el contador es 0', async () => {
      expect(
        await overdueOf({
          state: 'assigned',
          counts_as_overdue: true,
          due_at: '2026-01-02T15:00:00.000Z',
        }),
      ).toEqual({
        count: count(0),
        overdue: [],
        live: [{ label: text('scr09.overdue.due_on'), date: '02/01/2026' }],
      });
    });
  });
});
