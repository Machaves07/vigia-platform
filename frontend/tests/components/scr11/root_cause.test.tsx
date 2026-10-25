/**
 * SCR-11 (IF-10; BR-APP-34, 43, 45, 47, 49): inicio explícito, porqués guardados en el servidor,
 * avisos anclados de U-04, «pendiente de información», firma y asistente al lado de la plantilla.
 * Las aserciones comparan claves del catálogo, etiquetas de las unidades dueñas, códigos y nombres
 * accesibles exactos; nunca subcadenas de texto con datos (VIG-194).
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import Page from '@/pages/scr11/root_cause';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import loopLabels from '../../../../backend/resources/labels.loop.es.json';
import {
  advance,
  analysis,
  apiError,
  fakeLoop,
  LICENSE,
  providers,
  question,
  readyRequest,
  RECORD,
  renderAt,
  WAIT,
} from './support';

const detail = loopLabels.loop_detail_code;

async function setup(options: Parameters<typeof providers>[0] = {}) {
  const env = await providers(options);
  const loop = fakeLoop();
  loop.install(env.clock);
  const view = renderAt(<Page />, env.Providers);
  return { ...env, loop, view };
}

async function start() {
  fireEvent.click(screen.getByRole('button', { name: text('scr11.start') }));
  return screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
}

function statement(index: number) {
  return screen.getByRole('textbox', { name: `${text('scr11.statement')} ${String(index)}` });
}

async function saveWhy(index: number, value: string) {
  fireEvent.change(statement(index), { target: { value } });
  fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
  await screen.findByText(text('scr11.step_saved'), { selector: '[data-part="step-sync"]' }, WAIT);
}

function next() {
  fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.next') }));
}

async function toSummary() {
  await saveWhy(1, 'Primer porqué sintético con más de veinte caracteres.');
  next();
  for (let index = 2; index <= 5; index += 1) next();
  fireEvent.change(screen.getByRole('combobox', { name: text('scr11.root_category') }), {
    target: { value: 'engineering' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: text('scr11.root_statement') }), {
    target: { value: 'Enunciado sintético de la causa raíz de ingeniería.' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: text('scr11.conclusion') }), {
    target: { value: 'Conclusión sintética del análisis de la zona.' },
  });
  next();
  return screen.findByRole('region', { name: text('components_forms.signature.title') }, WAIT);
}

describe('SCR-11 causa raíz', { timeout: 60_000 }, () => {
  it('inicia solo por una acción explícita y muestra la plantilla con el contexto de la zona', async () => {
    const { loop } = await setup();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('navigation.scr11'));
    await act(async () => {
      await Promise.resolve();
    });
    expect(loop.calls.start).toBe(0);
    await start();
    expect(loop.calls.start).toBe(1);
    const context = screen.getByRole('region', { name: text('scr11.context_title') });
    expect(within(context).getByText(label('predicate_family', 'coexistence')).dataset.part).toBe(
      'family',
    );
    expect(context.querySelector('[data-part="recurrences"]')?.textContent).toBe('2');
    expect(context.querySelector('[data-part="previous-cause"]')?.textContent).toBe(
      `${label('cause_category', 'process')}: 1`,
    );
    expect(screen.getByText(question(1), { exact: false }).dataset.part).toBe('question');
    expect(
      screen.getByRole('button', { name: text('components_structure.wizard.next') }),
    ).toHaveProperty('disabled', true);
  });

  it('503 loop_standard_text_unavailable degrada el inicio y permite reintentar (diferencia 8)', async () => {
    const { loop } = await setup();
    loop.state.startResponse = () =>
      apiError('temporarily_unavailable', 503, 'loop_standard_text_unavailable');
    fireEvent.click(screen.getByRole('button', { name: text('scr11.start') }));
    const alert = await screen.findByRole('alert', {}, WAIT);
    expect(alert.textContent).toBe(text('scr11.standard_text_unavailable'));
    expect(screen.queryByRole('region', { name: text('scr11.wizard_title') })).toBeNull();
    loop.state.startResponse = null;
    fireEvent.click(screen.getByRole('button', { name: text('scr11.retry_start') }));
    await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
  });

  it('un hallazgo fuera del alcance muestra la pantalla uniforme', async () => {
    const { loop } = await setup();
    loop.state.startResponse = () => apiError('not_found', 404);
    fireEvent.click(screen.getByRole('button', { name: text('scr11.start') }));
    await screen.findByText(text('errors.not_available_in_scope'), {}, WAIT);
  });

  it('cada porqué se guarda con PUT y solo entonces deja avanzar; los siguientes pueden quedar en blanco', async () => {
    const { loop } = await setup();
    await start();
    const advanceButton = () =>
      screen.getByRole('button', { name: text('components_structure.wizard.next') });
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético de la zona.' } });
    expect(advanceButton()).toHaveProperty('disabled', true);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr11.category') }), {
      target: { value: 'process' },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    await screen.findByText(
      text('scr11.step_saved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    expect(loop.calls.steps).toEqual([
      {
        index: 1,
        body: {
          statement: 'Primer porqué sintético de la zona.',
          evidence_refs: [],
          category: 'process',
        },
      },
    ]);
    expect(advanceButton()).toHaveProperty('disabled', false);
    next();
    expect(screen.getByText(question(2), { exact: false }).dataset.part).toBe('question');
    expect(advanceButton()).toHaveProperty('disabled', false);
  });

  it('loop_analysis_step_out_of_order queda anclado al porqué y conserva lo escrito', async () => {
    const { loop } = await setup();
    await start();
    await saveWhy(1, 'Primer porqué sintético de la zona.');
    next();
    next();
    fireEvent.change(statement(3), { target: { value: 'Tercer porqué sin el segundo.' } });
    fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    const anchored = await screen.findByRole('alert', {}, WAIT);
    expect(anchored.querySelector('[data-part="message"]')?.textContent).toBe(
      detail.loop_analysis_step_out_of_order,
    );
    expect(statement(3).getAttribute('aria-describedby')).toBe(anchored.id);
    expect((statement(3) as HTMLTextAreaElement).value).toBe('Tercer porqué sin el segundo.');
    expect(loop.calls.steps.map((call) => call.index)).toEqual([1, 3]);
  });

  it('un rechazo del vocabulario muestra el término y la sugerencia exactos de la respuesta', async () => {
    const { loop } = await setup();
    await start();
    const term = 'término-sintético-7Qx';
    const suggestion = 'alternativa sintética 41Z';
    loop.state.stepResponse = () =>
      apiError('invalid_request', 400, 'loop_blocked_vocabulary_term', {
        rejection: { code: 'blocked_vocabulary_term', term, suggestion },
      });
    fireEvent.change(statement(1), {
      target: { value: 'Texto sintético rechazado por el validador.' },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    const rejection = await screen.findByText(
      text('components_assistant.rejection.blocked_vocabulary_term'),
      { selector: '[data-part="rejection-code"]' },
      WAIT,
    );
    const box = rejection.closest('[data-part="rejection"]');
    expect(box?.querySelector('[data-part="rejection-term"]')?.textContent).toBe(term);
    expect(box?.querySelector('[data-part="rejection-suggestion"]')?.textContent).toBe(suggestion);
  });

  it('sin término ni sugerencia en la respuesta (U-04 hoy) solo muestra el código, sin inventarlos', async () => {
    const { loop } = await setup();
    await start();
    loop.state.stepResponse = () =>
      apiError('invalid_request', 400, 'loop_person_attribution_detected');
    fireEvent.change(statement(1), {
      target: { value: 'Texto sintético que atribuye a alguien.' },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    const code = await screen.findByText(
      text('components_assistant.rejection.person_attribution_detected'),
      { selector: '[data-part="rejection-code"]' },
      WAIT,
    );
    const box = code.closest('[data-part="rejection"]');
    expect(box?.querySelector('[data-part="rejection-term"]')).toBeNull();
    expect(box?.querySelector('[data-part="rejection-suggestion"]')).toBeNull();
    expect(box?.querySelector('[data-part="rejection-no-detail"]')?.textContent).toBe(
      text('components_assistant.rejection.no_detail'),
    );
  });

  it('«guardar sin terminar» existe con server_progress_ref y guarda los porqués con cambios', async () => {
    const { loop } = await setup();
    await start();
    fireEvent.change(statement(1), {
      target: { value: 'Porqué sintético guardado sin terminar.' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.wizard.save_partial') }),
    );
    const status = await screen.findByText(
      text('components_structure.wizard.saved_partial'),
      { selector: '[data-part="outcome"]' },
      WAIT,
    );
    expect(status.getAttribute('role')).toBe('status');
    expect(loop.calls.steps.map((call) => call.index)).toEqual([1]);
  });

  it('firma con la licencia del perfil y muestra el análisis firmado; «Enviar» no duplica la firma', async () => {
    const { loop } = await setup();
    await start();
    const block = await toSummary();
    expect(block.querySelector('[data-part="license"]')?.textContent).toBe(LICENSE);
    fireEvent.click(
      within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.wizard.submit') }),
    );
    const signed = await screen.findByRole('region', { name: text('scr11.signed_title') }, WAIT);
    expect(loop.calls.signatures).toEqual([
      {
        professional_license: LICENSE,
        root_cause_category: 'engineering',
        root_cause_statement: 'Enunciado sintético de la causa raíz de ingeniería.',
        conclusion: 'Conclusión sintética del análisis de la zona.',
        cause_effect_branches: [],
      },
    ]);
    expect(signed.querySelector('[data-part="record"]')?.textContent).toBe(RECORD);
    expect(screen.getByText(label('root_cause_analysis_state', 'signed')).dataset.part).toBe(
      'analysis-state',
    );
  });

  it('409 loop_transition_not_allowed (ya firmado, diferencia 9) queda anclado a la firma', async () => {
    const { loop } = await setup();
    await start();
    const block = await toSummary();
    loop.state.signatureResponse = () => apiError('conflict', 409, 'loop_transition_not_allowed');
    fireEvent.click(
      within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    await waitFor(() => {
      expect(block.querySelector('[role="alert"] [data-part="message"]')?.textContent).toBe(
        detail.loop_transition_not_allowed,
      );
    }, WAIT);
  });

  it('loop_analysis_information_insufficient deja el análisis pendiente de información con lo que falta', async () => {
    const { loop } = await setup();
    await start();
    const block = await toSummary();
    loop.state.signatureResponse = () =>
      apiError('invalid_request', 400, 'loop_analysis_information_insufficient');
    fireEvent.click(
      within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    const pending = await screen.findByRole('region', { name: text('scr11.pending_title') }, WAIT);
    // El servidor esperaba las tres piezas finales y la firma las envió: la lista sale de su
    // `pending_information` menos lo enviado, nunca de una regla propia; sin lista, lo dice.
    expect(pending.querySelectorAll('[data-part="missing-item"]')).toHaveLength(0);
    expect(pending.querySelector('[data-part="missing-unknown"]')?.textContent).toBe(
      text('scr11.pending_unknown'),
    );
    expect(block.querySelector('[role="alert"] [data-part="message"]')?.textContent).toBe(
      detail.loop_analysis_information_insufficient,
    );
  });

  it('«pendiente de información» del servidor enumera lo que falta con sus etiquetas', async () => {
    const { loop } = await setup();
    loop.state.startResponse = () =>
      HttpResponse.json(
        analysis({
          state: 'pending_information',
          pending_information: ['root_cause_statement', 'conclusion'],
        }),
        { status: 201 },
      );
    await start();
    const pending = screen.getByRole('region', { name: text('scr11.pending_title') });
    expect(
      [...pending.querySelectorAll('[data-part="missing-item"]')].map((li) => li.textContent),
    ).toEqual([
      label('analysis_missing_item', 'root_cause_statement'),
      label('analysis_missing_item', 'conclusion'),
    ]);
  });

  it('sin licencia en el perfil advierte, envía la firma y muestra la guía de loop_signer_license_missing', async () => {
    const { loop } = await setup({ license: null });
    await start();
    const block = await toSummary();
    expect(block.querySelector('[data-part="warning"]')?.textContent).toBe(
      text('components_forms.signature.warning_no_license'),
    );
    fireEvent.click(
      within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    await waitFor(() => {
      expect(block.querySelector('[data-part="guide"]')?.textContent).toBe(
        text('components_forms.signature.guide_license_missing'),
      );
    }, WAIT);
    expect(loop.calls.signatures[0]?.professional_license).toBeNull();
  });

  it('con el asistente caído la plantilla sigue entera y se puede avanzar', async () => {
    const { loop } = await setup();
    await start();
    loop.state.assistantResponse = () =>
      apiError('temporarily_unavailable', 503, 'loop_assistant_unavailable');
    fireEvent.click(screen.getByRole('button', { name: text('components_assistant.request') }));
    await screen.findByText(text('components_assistant.unavailable'), {}, WAIT);
    expect(screen.getByRole('region', { name: text('scr11.wizard_title') })).toBeDefined();
    await saveWhy(1, 'Primer porqué sintético con el asistente caído.');
    expect(
      screen.getByRole('button', { name: text('components_structure.wizard.next') }),
    ).toHaveProperty('disabled', false);
  });

  it('el borrador listo se copia a la causa raíz para editarlo y nunca firma', async () => {
    const { loop, clock } = await setup();
    await start();
    loop.state.statusSteps = [() => HttpResponse.json(readyRequest())];
    fireEvent.click(screen.getByRole('button', { name: text('components_assistant.request') }));
    await waitFor(() => {
      expect(loop.calls.assistantRequests).toBe(1);
    }, WAIT);
    await advance(clock, 5_000);
    fireEvent.click(
      await screen.findByRole('button', { name: text('components_assistant.use_draft') }, WAIT),
    );
    expect(loop.calls.signatures).toEqual([]);
    await saveWhy(1, 'Primer porqué sintético antes del borrador.');
    for (let index = 1; index <= 5; index += 1) next();
    expect(
      screen.getByRole<HTMLTextAreaElement>('textbox', { name: text('scr11.root_statement') })
        .value,
    ).toBe(readyRequest().draft?.draft_text);
    expect(
      screen.getByRole<HTMLSelectElement>('combobox', { name: text('scr11.root_category') }).value,
    ).toBe('engineering');
  });

  it('axe sin violaciones con el borrador, un rechazo anclado y el contexto', async () => {
    const { loop, clock, view } = await setup();
    await start();
    loop.state.statusSteps = [() => HttpResponse.json(readyRequest())];
    fireEvent.click(screen.getByRole('button', { name: text('components_assistant.request') }));
    await waitFor(() => {
      expect(loop.calls.assistantRequests).toBe(1);
    }, WAIT);
    await advance(clock, 5_000);
    await screen.findByRole('article', { name: text('components_assistant.draft_mark') }, WAIT);
    loop.state.stepResponse = () =>
      apiError('invalid_request', 400, 'loop_blocked_vocabulary_term', {
        rejection: { code: 'blocked_vocabulary_term', term: 'término', suggestion: 'alternativa' },
      });
    fireEvent.change(statement(1), {
      target: { value: 'Texto sintético rechazado por el validador.' },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(await axe(view.container)).toHaveNoViolations();
  });
});
