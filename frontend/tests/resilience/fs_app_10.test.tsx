/**
 * FS-APP-10 (PAT-APP-RES-03, PR-APP-14, UI-22): el asistente responde `unavailable` o
 * `rejected_by_validators`.
 *
 * El análisis de causa raíz de SCR-11 con la plantilla guiada y el asistente al lado. Pedido un
 * borrador, el servidor lo da por no disponible (con una causa de la lista) o lo rechaza por sus
 * validadores (con el término y la sugerencia). En los dos casos: «continúe con la plantilla
 * guiada», la consulta de estado se detiene después de la respuesta final, y la plantilla sigue
 * funcionando (el porqué se guarda en el servidor). Nada se firma ni se escribe por el asistente.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import Page from '@/pages/scr11/root_cause';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import {
  advance,
  assistantRequest,
  fakeLoop,
  providers,
  renderAt,
  WAIT,
} from '../components/scr11/support';
import { integer, scenarioCase, syntheticText } from './harness';

const CAUSES = ['no_api_key', 'circuit_open', 'timeout', 'provider_error', 'budget_exceeded'];
const POLL_MS = 5_000;

async function setup() {
  const env = await providers();
  const loop = fakeLoop();
  loop.install(env.clock);
  renderAt(<Page />, env.Providers);
  fireEvent.click(await screen.findByRole('button', { name: text('scr11.start') }, WAIT));
  await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
  const panel = () => screen.getByRole('region', { name: text('components_assistant.title') });
  return { ...env, loop, panel };
}

/** Pide un borrador y espera a que el servidor lo acepte (desde ahí cuenta la consulta). */
async function requestDraft(loop: ReturnType<typeof fakeLoop>) {
  fireEvent.click(screen.getByRole('button', { name: text('components_assistant.request') }));
  await waitFor(() => {
    expect(loop.calls.assistantRequests).toBe(1);
  }, WAIT);
}

/** La plantilla guiada sigue: el primer porqué se guarda en el servidor. */
async function expectGuidedTemplate(loop: ReturnType<typeof fakeLoop>, statement: string) {
  const field = screen.getByRole('textbox', { name: `${text('scr11.statement')} 1` });
  fireEvent.change(field, { target: { value: statement } });
  fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
  await screen.findByText(text('scr11.step_saved'), { selector: '[data-part="step-sync"]' }, WAIT);
  expect(loop.calls.steps.map((step) => [step.index, step.body.statement])).toEqual([
    [1, statement],
  ]);
  expect(loop.calls.signatures).toEqual([]);
}

describe('FS-APP-10 · asistente unavailable y rejected_by_validators', { timeout: 120_000 }, () => {
  it('unavailable: plantilla guiada disponible, causa etiquetada y consulta detenida', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-10',
      fc.record({
        case: fc.constant('unavailable'),
        cause: fc.constantFrom(...CAUSES),
        pendingPolls: integer(0, 6),
        statement: syntheticText('Porqué sintético de proceso con detalle suficiente'),
      }),
    );
    const { loop, clock, panel } = await setup();
    loop.state.statusSteps = [
      ...Array.from(
        { length: parameters.pendingPolls },
        () => () => HttpResponse.json(assistantRequest()),
      ),
      () =>
        HttpResponse.json(
          assistantRequest({ assistant_state: 'unavailable', unavailable_cause: parameters.cause }),
        ),
    ];
    await requestDraft(loop);
    await advance(clock, (parameters.pendingPolls + 1) * POLL_MS + 1_000);
    const polls = loop.calls.statusAt.length;
    expect(polls).toBe(parameters.pendingPolls + 1);

    const notice = panel().querySelector('[data-part="assistant-unavailable"]');
    expect(notice?.querySelector('strong')?.textContent).toBe(
      text('components_assistant.unavailable'),
    );
    expect(notice?.querySelector('[data-part="unavailable-cause"]')?.textContent).toBe(
      `${text('components_assistant.unavailable_cause')}: ${label(
        'assistant_unavailable_cause',
        parameters.cause,
      )}`,
    );
    // Consulta detenida: dos minutos más sin ninguna petición de estado.
    await advance(clock, 120_000);
    expect(loop.calls.statusAt).toHaveLength(polls);
    await expectGuidedTemplate(loop, parameters.statement);
  });

  it('rejected_by_validators: término y sugerencia exactos, consulta detenida y plantilla guiada', async (context) => {
    const parameters = scenarioCase(
      context,
      'FS-APP-10',
      fc.record({
        case: fc.constant('rejected_by_validators'),
        code: fc.constantFrom('person_attribution_detected', 'blocked_vocabulary_term'),
        term: syntheticText('término sintético'),
        suggestion: syntheticText('redacte la causa como proceso o ingeniería'),
        pendingPolls: integer(0, 6),
        statement: syntheticText('Porqué sintético de ingeniería con detalle suficiente'),
      }),
    );
    const { loop, clock, panel } = await setup();
    const rejection = {
      code: parameters.code,
      term: parameters.term,
      suggestion: parameters.suggestion,
    };
    loop.state.statusSteps = [
      ...Array.from(
        { length: parameters.pendingPolls },
        () => () => HttpResponse.json(assistantRequest()),
      ),
      () =>
        HttpResponse.json({
          ...assistantRequest({ assistant_state: 'rejected_by_validators' }),
          rejection,
        }),
    ];
    await requestDraft(loop);
    await advance(clock, (parameters.pendingPolls + 1) * POLL_MS + 1_000);
    const polls = loop.calls.statusAt.length;
    expect(polls).toBe(parameters.pendingPolls + 1);

    const box = panel().querySelector('[data-part="rejection"]');
    expect({
      code: box?.querySelector('[data-part="rejection-code"]')?.textContent,
      term: box?.querySelector('[data-part="rejection-term"]')?.textContent,
      suggestion: box?.querySelector('[data-part="rejection-suggestion"]')?.textContent,
    }).toEqual({
      code: text(`components_assistant.rejection.${parameters.code}`),
      term: parameters.term,
      suggestion: parameters.suggestion,
    });
    await advance(clock, 120_000);
    expect(loop.calls.statusAt).toHaveLength(polls);
    await expectGuidedTemplate(loop, parameters.statement);
  });
});
