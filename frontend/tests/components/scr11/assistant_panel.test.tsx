/**
 * UI-22 `AssistantDraftPanel` (BR-APP-34, 49) con el puerto real de SCR-11 sobre MSW y el
 * `FakeClock` del transporte: consulta cada 5 s hasta 2 min, borrador siempre marcado con sus
 * citas, ninguna acción que firme o cierre, rechazo con los campos exactos de la respuesta.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { AssistantDraftPanel } from '@/components/AssistantDraftPanel/AssistantDraftPanel';
import type { AssistantDraft } from '@/components/AssistantDraftPanel/state';
import { useAssistantDraft } from '@/components/AssistantDraftPanel/useAssistantDraft';
import { assistantFailure, rootCauseClock, rootCausePort } from '@/ports/scr11RootCause';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import {
  ANALYSIS,
  apiError,
  assistantRequest,
  advance,
  fakeLoop,
  INSTANT,
  providers,
  readyRequest,
  WAIT,
} from './support';

const ACCEPTED_AT = Date.parse(INSTANT);

function Harness({ onUseDraft }: { readonly onUseDraft: (draft: AssistantDraft) => void }) {
  const assistant = useAssistantDraft({
    clock: rootCauseClock(),
    request: () => rootCausePort.requestAssistant.write({ path: { analysis_id: ANALYSIS } }),
    status: (request_id, signal) =>
      rootCausePort.assistantStatus.read({ path: { request_id } }, signal),
    classify: assistantFailure,
  });
  return (
    <AssistantDraftPanel
      state={assistant.state}
      onRequest={assistant.request}
      onUseDraft={onUseDraft}
    />
  );
}

async function setup() {
  const env = await providers();
  const loop = fakeLoop();
  loop.install(env.clock);
  const onUseDraft = vi.fn<(draft: AssistantDraft) => void>();
  const view = render(
    <env.Providers>
      <Harness onUseDraft={onUseDraft} />
    </env.Providers>,
  );
  const request = async () => {
    fireEvent.click(screen.getByRole('button', { name: text('components_assistant.request') }));
    await waitFor(() => {
      expect(loop.calls.assistantRequests).toBe(1);
    }, WAIT);
  };
  const panel = () => screen.getByRole('region', { name: text('components_assistant.title') });
  return { ...env, loop, view, onUseDraft, request, panel };
}

/** Nombres accesibles de todos los botones del panel. */
function buttonNames(panel: HTMLElement): string[] {
  return within(panel)
    .queryAllByRole('button')
    .map((button) => button.textContent);
}

describe('UI-22 AssistantDraftPanel', { timeout: 60_000 }, () => {
  it('consulta cada 5 s mientras está pending y se detiene en ready', async () => {
    const { loop, clock, request, panel } = await setup();
    loop.state.statusSteps = [
      () => HttpResponse.json(assistantRequest()),
      () => HttpResponse.json(assistantRequest()),
      () => HttpResponse.json(readyRequest()),
    ];
    await request();
    await advance(clock, 60_000);
    expect(loop.calls.statusAt.map((at) => at - ACCEPTED_AT)).toEqual([5_000, 10_000, 15_000]);
    expect(panel().dataset.status).toBe('ready');
  });

  it('el borrador lleva siempre la marca, sus citas exactas y ninguna acción que firme o cierre', async () => {
    const { loop, clock, request, panel, onUseDraft } = await setup();
    const response = readyRequest({ is_draft: false });
    loop.state.statusSteps = [() => HttpResponse.json(response)];
    await request();
    await advance(clock, 5_000);
    const draft = await screen.findByRole(
      'article',
      { name: text('components_assistant.draft_mark') },
      WAIT,
    );
    expect(draft.querySelector('[data-part="draft-mark"]')?.textContent).toBe(
      text('components_assistant.draft_mark'),
    );
    expect(draft.querySelector('[data-part="draft-text"]')?.textContent).toBe(
      response.draft?.draft_text,
    );
    const citations = [...draft.querySelectorAll('[data-part="citation"]')].map((item) => ({
      kind: item.querySelector('[data-part="citation-kind"]')?.textContent,
      fact: item.querySelector('[data-part="citation-fact"]')?.textContent,
      alias: item.querySelector('[data-part="citation-alias"]')?.textContent,
    }));
    expect(citations).toEqual(
      (response.draft?.citations ?? []).map((citation) => ({
        kind: label('evidence_ref_kind', citation.kind),
        fact: citation.quoted_fact_es,
        alias: citation.id,
      })),
    );
    expect(draft.querySelector('[data-part="proposed-category"]')?.textContent).toBe(
      label('cause_category', 'engineering'),
    );
    // Árbol accesible: las únicas acciones son pedir y copiar a un campo editable.
    expect(buttonNames(panel())).toEqual([
      text('components_assistant.request'),
      text('components_assistant.use_draft'),
    ]);
    expect(within(panel()).queryAllByRole('link')).toEqual([]);
    expect(within(panel()).queryAllByRole('checkbox')).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: text('components_assistant.use_draft') }));
    expect(onUseDraft).toHaveBeenCalledTimes(1);
    expect(onUseDraft.mock.calls[0]?.[0].draft_text).toBe(response.draft?.draft_text);
    expect(screen.getByText(text('components_assistant.draft_copied')).getAttribute('role')).toBe(
      'status',
    );
    expect(await axe(panel())).toHaveNoViolations();
  });

  it('unavailable detiene la consulta y remite a la plantilla guiada con la causa etiquetada', async () => {
    const { loop, clock, request, panel } = await setup();
    loop.state.statusSteps = [
      () =>
        HttpResponse.json(
          assistantRequest({ assistant_state: 'unavailable', unavailable_cause: 'circuit_open' }),
        ),
    ];
    await request();
    await advance(clock, 60_000);
    expect(loop.calls.statusAt).toHaveLength(1);
    const notice = panel().querySelector('[data-part="assistant-unavailable"]');
    expect(notice?.querySelector('strong')?.textContent).toBe(
      text('components_assistant.unavailable'),
    );
    expect(notice?.querySelector('[data-part="unavailable-cause"]')?.textContent).toBe(
      `${text('components_assistant.unavailable_cause')}: ${label(
        'assistant_unavailable_cause',
        'circuit_open',
      )}`,
    );
  });

  it('a los 2 min sin respuesta final deja de consultar y lo dice', async () => {
    const { loop, clock, request, panel } = await setup();
    await request();
    await advance(clock, 300_000);
    // Consultas a los 5, 10, …, 115 s: ninguna a los 120 s ni después.
    expect(loop.calls.statusAt.map((at) => at - ACCEPTED_AT)).toEqual(
      Array.from({ length: 23 }, (_, index) => (index + 1) * 5_000),
    );
    expect(panel().querySelector('[data-part="expired"]')?.textContent).toBe(
      text('components_assistant.expired'),
    );
    expect(panel().dataset.status).toBe('unavailable');
  });

  it('503 al pedir: no disponible de inmediato, sin ninguna consulta', async () => {
    const { loop, clock, request, panel } = await setup();
    loop.state.assistantResponse = () =>
      apiError('temporarily_unavailable', 503, 'loop_assistant_unavailable');
    await request();
    await advance(clock, 30_000);
    expect(loop.calls.statusAt).toEqual([]);
    expect(panel().dataset.status).toBe('unavailable');
  });

  it('400 loop_assistant_output_rejected sin detalle: rechazo sin término ni sugerencia inventados', async () => {
    const { loop, clock, request, panel } = await setup();
    loop.state.statusSteps = [
      () => apiError('invalid_request', 400, 'loop_assistant_output_rejected'),
    ];
    await request();
    await advance(clock, 30_000);
    expect(loop.calls.statusAt).toHaveLength(1);
    const rejected = panel().querySelector('[data-part="assistant-rejected"]');
    expect(rejected?.querySelector('strong')?.textContent).toBe(
      text('components_assistant.rejected'),
    );
    expect(rejected?.querySelector('[data-part="rejection-code"]')?.textContent).toBe(
      text('components_assistant.rejection.unknown'),
    );
    expect(rejected?.querySelector('[data-part="rejection-term"]')).toBeNull();
    expect(rejected?.querySelector('[data-part="rejection-no-detail"]')?.textContent).toBe(
      text('components_assistant.rejection.no_detail'),
    );
  });

  it('rejected_by_validators con la forma del diseño muestra el término y la sugerencia exactos', async () => {
    const { loop, clock, request, panel } = await setup();
    const rejection = {
      code: 'person_attribution_detected',
      term: 'término sintético 9Kd',
      suggestion: 'redacte la causa como proceso, ingeniería u organización 3Wq',
    };
    loop.state.statusSteps = [
      () =>
        HttpResponse.json({
          ...assistantRequest({ assistant_state: 'rejected_by_validators' }),
          rejection,
        }),
    ];
    await request();
    await advance(clock, 30_000);
    expect(loop.calls.statusAt).toHaveLength(1);
    const box = panel().querySelector('[data-part="rejection"]');
    expect({
      code: box?.querySelector('[data-part="rejection-code"]')?.textContent,
      term: box?.querySelector('[data-part="rejection-term"]')?.textContent,
      suggestion: box?.querySelector('[data-part="rejection-suggestion"]')?.textContent,
    }).toEqual({
      code: text('components_assistant.rejection.person_attribution_detected'),
      term: rejection.term,
      suggestion: rejection.suggestion,
    });
    expect(buttonNames(panel())).toEqual([text('components_assistant.request')]);
    expect(await axe(panel())).toHaveNoViolations();
  });

  it('tras un estado final se puede pedir otro borrador', async () => {
    const { loop, clock, request } = await setup();
    loop.state.statusSteps = [
      () => HttpResponse.json(assistantRequest({ assistant_state: 'unavailable' })),
    ];
    await request();
    await advance(clock, 10_000);
    const button = screen.getByRole('button', { name: text('components_assistant.request') });
    expect(button).toHaveProperty('disabled', false);
    fireEvent.click(button);
    await waitFor(() => {
      expect(loop.calls.assistantRequests).toBe(2);
    }, WAIT);
  });
});
