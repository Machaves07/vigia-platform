import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import {
  CLASSIFICATION_REASON_LIMITS,
  type ClassificationBody,
  type ClassificationItem,
} from '@/ports/scr09Classify';
import { RESUMING_STATE as RESUMING } from '@/pages/shared/FindingActions';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
import {
  classification,
  detail,
  FINDING,
  history,
  INSTANT,
  renderScreen,
  WAIT,
} from './classify_support';

const REASON = 'Reflejo del sol sobre la guarda en la tarde, revisado en el clip';
const REASON_NAME = `${text('scr09.classify.reason_text')} (${text('components_forms.reason.required')})`;
const validators = openApiValidators(
  (readAppYaml().components as { schemas: Record<string, unknown> }).schemas,
);

/** Servidor con historial propio: cada POST aceptado añade un registro nuevo al final. */
function loopServer(initial: ClassificationItem[], state = 'open') {
  const items = [...initial];
  const bodies: unknown[] = [];
  const reads = { detail: 0, history: 0 };
  server.use(
    http.get(`http://localhost/loop/findings/${FINDING}`, () => {
      reads.detail++;
      return HttpResponse.json(
        detail({
          lifecycle_state: state,
          classifications: items,
          classification_count: items.length,
        }),
      );
    }),
    http.get(`http://localhost/loop/findings/${FINDING}/classifications`, () => {
      reads.history++;
      return HttpResponse.json(history(items));
    }),
    http.post(`http://localhost/loop/findings/${FINDING}/classifications`, async ({ request }) => {
      const body = (await request.json()) as ClassificationBody;
      bodies.push(body);
      const validate = validators.component('ClassificationBody');
      if (!validate(body)) return apiError('invalid_request', 400);
      const next = classification(items.length + 1, body.outcome ?? '');
      items.push({
        ...next,
        reason_category: body.reason_category ?? '',
        reason_text: body.reason_text ?? '',
        reinforced_reason: body.reinforced_reason,
      });
      return HttpResponse.json(
        {
          finding_state_id: FINDING,
          classification_id: next.classification_id,
          record_id: next.record_id,
          sequence_in_subject: next.sequence_in_subject,
          outcome: next.outcome,
          reason_category: next.reason_category,
          is_reclassification: next.is_reclassification,
          reinforced_reason: body.reinforced_reason,
          effect_on_loop: 'loop_suspended',
          lifecycle_state: 'suspended_by_reclassification',
          classification_count: items.length,
          closed_with_later_different_classification: false,
          classified_at: INSTANT,
          time_aggregated: false,
        },
        { status: 201 },
      );
    }),
  );
  return { items, bodies, reads };
}

async function load() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: text('scr09.classify.load') }, WAIT));
  await screen.findByRole('form', { name: text('scr09.classify.form') }, WAIT);
  return user;
}

describe('SCR-09 clasificación', { timeout: 40_000 }, () => {
  it('los límites del motivo son los de classification/reason_text del corpus exportado', () => {
    const path = resolve(
      dirname(fileURLToPath(import.meta.url)),
      '../../../../backend/openapi/u05-fixtures.json',
    );
    const fixtures = JSON.parse(readFileSync(path, 'utf8')) as {
      free_text: { routes: { route: string; min_length: number; max_length: number }[] };
    };
    const route = fixtures.free_text.routes.find(
      (item) => item.route === 'classification/reason_text',
    );
    expect(CLASSIFICATION_REASON_LIMITS).toEqual({
      minLength: route?.min_length,
      maxLength: route?.max_length,
    });
  });

  it('no pide lo auditado al montar; solo al pulsar consultar', async () => {
    const { reads } = loopServer([classification(1)]);
    const { cache } = await renderScreen('classify');
    await screen.findByRole('button', { name: text('scr09.classify.load') }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(reads).toEqual({ detail: 0, history: 0 });
    await load();
    expect(reads).toEqual({ detail: 1, history: 1 });
  });

  it('reclasificar con el lazo abierto avisa la suspensión, pide el motivo reforzado y registra uno nuevo sin borrar el anterior', async () => {
    const { bodies } = loopServer([classification(1)], 'action_assigned');
    const { container, cache } = await renderScreen('classify');
    const user = await load();
    const list = screen.getByRole('list', { name: text('scr09.classify.history') });
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
    expect(container.querySelector('[data-part="suspends"]')?.textContent).toBe(
      text('scr09.classify.suspends'),
    );
    await user.click(
      screen.getByRole('radio', { name: label('classification_outcome', 'false_positive') }),
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr09.classify.reason_category') }),
      'reflection_or_glare',
    );
    await user.type(screen.getByRole('textbox', { name: REASON_NAME }), REASON);
    await user.click(screen.getByRole('checkbox', { name: text('scr09.classify.reinforced') }));
    await user.click(screen.getByRole('button', { name: text('scr09.classify.submit') }));
    await waitFor(() => {
      expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    }, WAIT);
    expect(bodies).toEqual([
      {
        outcome: 'false_positive',
        reason_category: 'reflection_or_glare',
        reason_text: REASON,
        reinforced_reason: true,
      },
    ]);
    const items = within(list).getAllByRole('listitem');
    expect(
      items.map((item) => item.querySelector('[data-part="reason_text"]')?.textContent),
    ).toEqual([classification(1).reason_text, REASON]);
    const receipt = await screen.findByText(
      `${text('scr09.classify.receipt')} 2: ${label('classification_outcome', 'false_positive')}`,
      { selector: '[data-part="record"]' },
      WAIT,
    );
    expect(receipt.closest('[role="status"]')?.getAttribute('data-role-in-use')).toBe(
      'coordinator_sst',
    );
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    // BR-APP-24: los únicos controles son consultar, actualizar y registrar; ningún editar o borrar.
    const allowed = new Set([
      text('scr09.classify.load'),
      text('scr09.classify.submit'),
      text('components_status.refresh.button'),
    ]);
    for (const button of screen.getAllByRole('button'))
      expect(allowed.has(button.textContent)).toBe(true);
    for (const link of screen.getAllByRole('link'))
      expect([text('scr09.classify.title'), text('scr09.loop.title')]).toContain(link.textContent);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('un motivo rechazado por el servidor se ancla al campo y conserva lo escrito', async () => {
    // Primera clasificación: solo existe desde `received` (T03/T04), sin motivo reforzado.
    loopServer([], 'received');
    server.use(
      http.post(`http://localhost/loop/findings/${FINDING}/classifications`, () =>
        apiError('invalid_request', 400, { detailCode: 'loop_person_attribution_detected' }),
      ),
    );
    await renderScreen('classify');
    const user = await load();
    expect(screen.queryByRole('checkbox')).toBeNull();
    await user.click(
      screen.getByRole('radio', { name: label('classification_outcome', 'confirmed') }),
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr09.classify.reason_category') }),
      'procedure_gap',
    );
    const field = screen.getByRole<HTMLTextAreaElement>('textbox', { name: REASON_NAME });
    await user.type(field, REASON);
    await user.click(screen.getByRole('button', { name: text('scr09.classify.submit') }));
    await waitFor(() => {
      expect(field.getAttribute('aria-invalid')).toBe('true');
    }, WAIT);
    const notice = document.getElementById(
      field.getAttribute('aria-describedby')?.split(' ')[1] ?? '',
    );
    expect(notice?.getAttribute('data-code')).toBe('invalid_request');
    expect(notice?.querySelector('[data-part="message"]')?.textContent).toBe(
      label('loop_detail_code', 'loop_person_attribution_detected'),
    );
    expect(field.value).toBe(REASON);
    const category = screen.getByRole<HTMLSelectElement>('combobox', {
      name: text('scr09.classify.reason_category'),
    });
    expect(category.value).toBe('procedure_gap');
  });

  // --- VIG-359: aviso previo por estado y resolución de la cola en el historial ----------------

  const DETECTION = '0190a1b2-4444-7000-8000-000000000359';

  /** La parte `data-part` del formulario; falla (y `waitFor` reintenta) mientras no está. */
  function part(form: HTMLElement, name: string): HTMLElement {
    const found = form.querySelector<HTMLElement>(`[data-part="${name}"]`);
    if (found === null) throw new Error(`Falta la parte ${name} del formulario.`);
    return found;
  }

  function describedBy(form: HTMLElement): string[] {
    return (form.getAttribute('aria-describedby') ?? '').split(' ').filter(Boolean);
  }

  it('VIG-359: en revisión avisa antes de enviar y enlaza esa detección en la cola, sin 409', async () => {
    const { bodies } = loopServer([], 'in_review');
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}`, () =>
        HttpResponse.json(
          detail({
            lifecycle_state: 'in_review',
            subject_kind: 'detection',
            subject_id: DETECTION,
            classifications: [],
            classification_count: 0,
          }),
        ),
      ),
    );
    const { container } = await renderScreen('classify');
    await load();
    const form = screen.getByRole('form', { name: text('scr09.classify.form') });
    const note = await waitFor(() => part(form, 'in_review'), WAIT);
    expect(note.getAttribute('role')).toBe('note');
    expect(describedBy(form)).toContain(note.id);
    expect(note.querySelector('p')?.textContent).toBe(text('finding_journey.in_review'));
    const link = within(note).getByRole('link', { name: text('finding_journey.review_queue') });
    // La detección viaja en el estado de la navegación (lo comprueban el detalle y FindingActions).
    expect(link.getAttribute('href')).toBe('/review-queue');
    // El aviso llega antes de enviar nada: el servidor sigue siendo quien decide.
    expect(bodies).toHaveLength(0);
    expect(screen.getByRole('button', { name: text('scr09.classify.submit') })).toBeDefined();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('VIG-359: confirmado en la cola (abierto sin clasificaciones) es una reclasificación que suspende el lazo', async () => {
    const { bodies } = loopServer([], 'open');
    await renderScreen('classify');
    const user = await load();
    const form = screen.getByRole('form', { name: text('scr09.classify.form') });
    const state = await waitFor(() => part(form, 'reclassification'), WAIT);
    expect(describedBy(form)).toContain(state.id);
    expect(state.querySelector('p')?.textContent).toBe(text('finding_journey.reclassification'));
    expect(state.querySelector('[data-part="suspends"]')?.textContent).toBe(
      text('scr09.classify.suspends'),
    );
    expect(form.querySelector('[data-part="in_review"]')).toBeNull();
    await user.click(
      screen.getByRole('radio', { name: label('classification_outcome', 'false_positive') }),
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr09.classify.reason_category') }),
      'reflection_or_glare',
    );
    await user.type(screen.getByRole('textbox', { name: REASON_NAME }), REASON);
    await user.click(screen.getByRole('checkbox', { name: text('scr09.classify.reinforced') }));
    await user.click(screen.getByRole('button', { name: text('scr09.classify.submit') }));
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    }, WAIT);
    expect(bodies[0]).toEqual({
      outcome: 'false_positive',
      reason_category: 'reflection_or_glare',
      reason_text: REASON,
      reinforced_reason: true,
    });
  });

  it('VIG-359: un lazo suspendido avisa de que confirmar lo reanuda', async () => {
    loopServer([classification(1), classification(2, 'false_positive')], RESUMING);
    await renderScreen('classify');
    await load();
    const form = screen.getByRole('form', { name: text('scr09.classify.form') });
    const resumes = await waitFor(() => part(form, 'resumes'), WAIT);
    expect(resumes.textContent).toBe(text('finding_journey.resumes'));
    expect(form.querySelector('[data-part="suspends"]')).toBeNull();
  });

  it('VIG-359: el historial muestra la resolución de la cola aparte de las clasificaciones', async () => {
    loopServer([], 'open');
    const resolution = {
      resolution_id: '0190a1b2-cccc-7000-8000-000000000011',
      record_id: '0190a1b2-cccc-7000-8000-000000000012',
      outcome: 'review_confirmed',
      reason_category: 'real_coexistence',
      reason_text: 'Coexistencia sintética confirmada en el clip de la detección revisada.',
      resolved_at: INSTANT,
      resolved_by: {
        user_id: '0190a1b2-8888-7000-8000-000000000011',
        display_name: 'Revisión sintética',
        role_in_use: 'coordinator_sst',
      },
    };
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}/classifications`, () =>
        HttpResponse.json({ ...history([]), review_resolutions: [resolution] }),
      ),
    );
    const { container } = await renderScreen('classify');
    await load();
    const historyRegion = screen.getByRole('region', { name: text('scr09.classify.history') });
    const section = within(historyRegion).getByRole('region', {
      name: text('finding_journey.resolution'),
    });
    const item = section.querySelector<HTMLElement>('[data-part="review_resolution"]');
    expect(item?.getAttribute('data-outcome')).toBe('review_confirmed');
    expect(item?.querySelector('strong')?.textContent).toBe(
      `${text('finding_journey.resolution_outcome')}: ${label('review_resolution_outcome', 'review_confirmed')}`,
    );
    expect(item?.querySelector('[data-part="reason_text"]')?.textContent).toBe(
      resolution.reason_text,
    );
    expect(item?.querySelector('[data-role-in-use]')?.getAttribute('data-role-in-use')).toBe(
      'coordinator_sst',
    );
    expect(historyRegion.querySelectorAll('[data-part="classification"]')).toHaveLength(0);
    expect(within(historyRegion).getByText(text('scr09.classify.history_empty'))).toBeDefined();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('SCR-09 clasificar con la evidencia a la vista (VIG-363)', { timeout: 40_000 }, () => {
  const EVIDENCE = '0190a1b2-5555-7000-8000-000000000363';

  function serveEvidence() {
    const reads = { readUrl: 0 };
    server.use(
      http.get(`http://localhost/loop/findings/${FINDING}`, () =>
        HttpResponse.json(
          detail({
            lifecycle_state: 'received',
            classifications: [],
            classification_count: 0,
            records: [
              {
                record_id: '0190a1b2-6666-7000-8000-000000000363',
                record_type: 'finding',
                chain_sequence: 7,
                content_hash: 'b'.repeat(64),
                segment_index: 0,
                received_at: INSTANT,
                occurred_at: INSTANT,
                signals: [],
                clips: [
                  {
                    evidence_id: EVIDENCE,
                    clip_id: '0190a1b2-5555-7000-8000-000000000364',
                    camera_id: '0190a1b2-5555-7000-8000-000000000365',
                    segment: 'full',
                    media_kind: 'video',
                    content_type: 'video/mp4',
                    size_bytes: 1024,
                    sha256: 'c'.repeat(64),
                    duration_ms: 5000,
                    starts_at: INSTANT,
                    ends_at: INSTANT,
                    read_url: null,
                  },
                ],
              },
            ],
          }),
        ),
      ),
      http.get(`http://localhost/loop/findings/${FINDING}/classifications`, () =>
        HttpResponse.json(history([])),
      ),
      http.post('http://localhost/evidence/:id/read-url', () => {
        reads.readUrl++;
        return HttpResponse.json({
          evidence_id: EVIDENCE,
          url: 'https://evidencia.example/clip-sintetico.mp4',
          expires_at: '2026-01-01T12:05:00.000Z',
        });
      }),
    );
    return reads;
  }

  async function consult(permissions: string[]) {
    const reads = serveEvidence();
    const rendered = await renderScreen('classify', { permissions });
    expect(rendered.container.querySelector('[data-part="finding_evidence"]')).toBeNull();
    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: text('scr09.classify.load') }, WAIT));
    const summary = await screen.findByRole('region', { name: text('scr08.detail.summary') }, WAIT);
    return { ...rendered, reads, summary };
  }

  it('el resumen y el clip aparecen con el hallazgo consultado; la URL solo al pulsar', async () => {
    const { reads, summary, container } = await consult([
      'findings.read',
      'findings.classify',
      'evidence.read',
      'hierarchy.read',
    ]);
    expect([...summary.querySelectorAll('dt')].map((node) => node.textContent)).toEqual([
      text('scr08.labels.zone'),
      text('scr08.detail.family'),
      text('scr08.detail.tier'),
      text('scr08.detail.state'),
      text('scr08.detail.occurred_at'),
      text('scr08.detail.standard'),
    ]);
    const clips = within(summary).getByRole('list', { name: text('scr08.detail.clips') });
    const play = within(clips).getByRole('button', { name: text('media.clip.play') });
    // El resumen va antes del formulario de la clasificación.
    const form = await screen.findByRole('form', { name: text('scr09.classify.form') }, WAIT);
    expect(summary.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(reads.readUrl).toBe(0);
    await userEvent.setup().click(play);
    await waitFor(() => {
      expect(reads.readUrl).toBe(1);
    }, WAIT);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('sin evidence.read, el resumen sin clips ni petición de URL', async () => {
    const { reads, summary } = await consult([
      'findings.read',
      'findings.classify',
      'hierarchy.read',
    ]);
    expect(within(summary).queryByRole('list', { name: text('scr08.detail.clips') })).toBeNull();
    expect(reads.readUrl).toBe(0);
  });
});
