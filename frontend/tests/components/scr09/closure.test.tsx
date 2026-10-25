import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { closurePort, readHistory } from '@/ports/scr09Closure';
import { text } from '@/texts';
import { apiError, exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
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
});
