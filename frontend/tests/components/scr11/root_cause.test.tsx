/**
 * SCR-11 (IF-10; BR-APP-34, 43, 45, 47, 49): inicio explícito, porqués guardados en el servidor,
 * avisos anclados de U-04, «pendiente de información», firma y asistente al lado de la plantilla.
 * Las aserciones comparan claves del catálogo, etiquetas de las unidades dueñas, códigos y nombres
 * accesibles exactos; nunca subcadenas de texto con datos (VIG-194).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import Page from '@/pages/scr11/root_cause';
import { ROOT_CAUSE_TEXT_LIMITS } from '@/ports/scr11RootCause';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import loopLabels from '../../../../backend/resources/labels.loop.es.json';
import { apiError as mswApiError } from '../../msw/handlers';
import { openApiValidators, readAppYaml } from '../../support/openapiAjv';
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

/** Rutas de texto libre del corpus exportado (sus sustitutos sueltos no los admite el JSON de Vite). */
const fixtures = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      '../../../../backend/openapi/u05-fixtures.json',
    ),
    'utf8',
  ),
) as {
  free_text: { routes: { route: string; min_length: number; max_length: number }[] };
};

/** Validador de `ApiErrorBody` contra `backend/openapi/app.yaml` (el archivo, no la copia). */
function errorBody() {
  const document = readAppYaml() as { components: { schemas: Record<string, unknown> } };
  return openApiValidators(document.components.schemas).component('ApiErrorBody');
}

async function setup(options: Parameters<typeof providers>[0] = {}) {
  const env = await providers(options);
  const loop = fakeLoop();
  loop.install(env.clock);
  const view = renderAt(<Page />, env.Providers);
  return { ...env, loop, view };
}

/** «Iniciar» aparece cuando la lectura del análisis en curso dice que no hay ninguno (VIG-363). */
function startButton() {
  return screen.findByRole('button', { name: text('scr11.start') }, WAIT);
}

async function start() {
  fireEvent.click(await startButton());
  return screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
}

function statement(index: number) {
  const requirement = index === 1 ? 'required' : 'optional';
  return screen.getByRole('textbox', {
    name: `${text('scr11.statement')} ${String(index)} (${text(`components_forms.reason.${requirement}`)})`,
  });
}

function rootName(key: string): string {
  return `${text(key)} (${text('components_forms.reason.required')})`;
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
  fireEvent.change(screen.getByRole('textbox', { name: rootName('scr11.root_statement') }), {
    target: { value: 'Enunciado sintético de la causa raíz de ingeniería.' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: rootName('scr11.conclusion') }), {
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
    fireEvent.click(await startButton());
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
    fireEvent.click(await startButton());
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
    expect(statement(3).getAttribute('aria-describedby')?.split(' ')).toContain(anchored.id);
    expect((statement(3) as HTMLTextAreaElement).value).toBe('Tercer porqué sin el segundo.');
    expect(loop.calls.steps.map((call) => call.index)).toEqual([1, 3]);
  });

  it('un rechazo del vocabulario muestra el término y la sugerencia exactos de la respuesta', async () => {
    const { loop } = await setup();
    await start();
    const term = 'término-sintético-7Qx';
    const suggestion = 'alternativa sintética 41Z';
    // La respuesta de MSW generada desde `app.yaml` (VIG-344: `ApiErrorBody.rejection`), y no un
    // cuerpo a mano: si U-04 cambia la forma, esta prueba cae.
    const response = mswApiError('invalid_request', 400, {
      detailCode: 'loop_blocked_vocabulary_term',
      rejection: { code: 'blocked_vocabulary_term', term, suggestion },
    });
    const body: unknown = await response.clone().json();
    const validate = errorBody();
    expect(validate(body), JSON.stringify(validate.errors)).toBe(true);
    loop.state.stepResponse = () => response.clone();
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

  it('sin término ni sugerencia en la respuesta (versión anterior de U-04) solo muestra el código, sin inventarlos', async () => {
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
      screen.getByRole<HTMLTextAreaElement>('textbox', { name: rootName('scr11.root_statement') })
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

describe('SCR-11 retoma el análisis en curso (VIG-363)', { timeout: 60_000 }, () => {
  const SAVED = [
    'Primer porqué sintético guardado en el servidor.',
    'Segundo porqué sintético guardado en el servidor.',
  ];

  function savedAnalysis() {
    return analysis({
      why_steps: SAVED.map((statement, offset) => ({
        step_index: offset + 1,
        question_es: question(offset + 1),
        statement,
        evidence_refs: [],
        category: null,
      })),
      next_step_index: 3,
      next_question_es: question(3),
      pending_information: ['root_cause_category', 'root_cause_statement', 'conclusion'],
    });
  }

  async function resumed(requestId: string | null) {
    const env = await providers();
    const loop = fakeLoop();
    loop.saved(savedAnalysis(), requestId);
    loop.install(env.clock);
    const view = renderAt(<Page />, env.Providers);
    const region = await screen.findByRole('region', { name: text('scr11.in_progress') }, WAIT);
    return { ...env, loop, view, region };
  }

  it('sin análisis en curso solo ofrece iniciar, y lo pide una sola vez al entrar', async () => {
    const { loop } = await setup();
    await startButton();
    expect(screen.queryByRole('button', { name: text('scr11.continue') })).toBeNull();
    expect(screen.queryByRole('region', { name: text('scr11.in_progress') })).toBeNull();
    expect(loop.calls.inProgress).toBe(1);
    expect(loop.calls.start).toBe(0);
  });

  it('con uno guardado muestra sus porqués y su borrador, y no ofrece iniciar otro', async () => {
    const { loop, region } = await resumed(readyRequest().request_id);
    expect(
      [...region.querySelectorAll('[data-part="summary-whys"] li')].map((li) => li.textContent),
    ).toEqual(SAVED);
    expect(region.querySelector('[data-part="analysis-state"]')?.textContent).toBe(
      label('root_cause_analysis_state', 'draft'),
    );
    expect(region.querySelector('[data-part="draft-notice"]')?.textContent).toBe(
      text('scr11.in_progress_draft'),
    );
    expect(screen.queryByRole('button', { name: text('scr11.start') })).toBeNull();
    expect(screen.queryByText(text('scr11.start_new_notice'))).toBeNull();
    expect(loop.calls.start).toBe(0);
  });

  it('«Continuar» retoma lo guardado y vuelve a mostrar el borrador marcado sin pedir otro', async () => {
    const { loop } = await resumed(readyRequest().request_id);
    loop.state.statusSteps = [() => HttpResponse.json(readyRequest())];
    fireEvent.click(screen.getByRole('button', { name: text('scr11.continue') }));
    await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
    expect((statement(1) as HTMLTextAreaElement).value).toBe(SAVED[0]);
    const draft = await screen.findByRole(
      'article',
      { name: text('components_assistant.draft_mark') },
      WAIT,
    );
    expect(draft.textContent).toContain(readyRequest().draft?.draft_text ?? '');
    expect(loop.calls.assistantRequests).toBe(0);
    expect(loop.calls.statusAt).toHaveLength(1);
    expect(loop.calls.start).toBe(0);
  });

  it('sin solicitud al asistente lo dice y «Continuar» no consulta ninguna', async () => {
    const { loop, region } = await resumed(null);
    expect(region.querySelector('[data-part="draft-notice"]')?.textContent).toBe(
      text('scr11.in_progress_no_draft'),
    );
    fireEvent.click(screen.getByRole('button', { name: text('scr11.continue') }));
    await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
    expect(loop.calls.statusAt).toEqual([]);
  });

  it('una lectura fuera del alcance responde la pantalla uniforme y no ofrece iniciar', async () => {
    const env = await providers();
    const loop = fakeLoop();
    loop.install(env.clock);
    loop.state.inProgressResponse = () => apiError('not_found', 404);
    renderAt(<Page />, env.Providers);
    await screen.findByText(text('errors.not_available_in_scope'), {}, WAIT);
    expect(screen.queryByRole('button', { name: text('scr11.start') })).toBeNull();
  });

  it('una lectura fallida se puede repetir y entonces muestra lo guardado', async () => {
    const env = await providers();
    const loop = fakeLoop();
    loop.saved(savedAnalysis());
    loop.install(env.clock);
    loop.state.inProgressResponse = () => apiError('internal_error', 500);
    renderAt(<Page />, env.Providers);
    const retry = await screen.findByRole('button', { name: text('screen_state.retry') }, WAIT);
    expect(screen.queryByRole('button', { name: text('scr11.start') })).toBeNull();
    loop.state.inProgressResponse = null;
    fireEvent.click(retry);
    await screen.findByRole('region', { name: text('scr11.in_progress') }, WAIT);
    expect(loop.calls.inProgress).toBe(2);
  });

  it('axe sin violaciones con el análisis en curso', async () => {
    const { view } = await resumed(readyRequest().request_id);
    expect(await axe(view.container)).toHaveNoViolations();
  });
});

// --- A-126 (TASK-580): textos de la causa raíz que guían en vez de rechazar ---------------------

const CHARACTERS = text('components_forms.reason.characters');
const MINIMUM = text('components_forms.reason.minimum');

function whyPart(index: number): HTMLElement {
  const part = document.querySelector<HTMLElement>(`[data-part="why-${String(index)}"]`);
  if (part === null) throw new Error(`falta el porqué ${String(index)}`);
  return part;
}

function saveButton() {
  return screen.getByRole('button', { name: text('scr11.save_step') });
}

/** El aviso anclado al campo: el elemento que el campo referencia y tiene `role="alert"`. */
function anchoredTo(field: HTMLElement): HTMLElement | null {
  const ids = field.getAttribute('aria-describedby')?.split(' ') ?? [];
  return (
    ids
      .map((id) => document.getElementById(id))
      .find((element) => element?.getAttribute('role') === 'alert') ?? null
  );
}

describe('SCR-11 A-126: textos que guían en vez de rechazar', { timeout: 60_000 }, () => {
  it('los límites de los textos son los de U-04 en el corpus exportado (igualdad)', () => {
    const routes = new Map(
      fixtures.free_text.routes.map((route) => [
        route.route,
        { minLength: route.min_length, maxLength: route.max_length },
      ]),
    );
    expect(ROOT_CAUSE_TEXT_LIMITS).toEqual({
      why_statement: routes.get('root_cause_analysis/why_steps[*]/statement'),
      root_cause_statement: routes.get('root_cause_analysis/root_cause_statement'),
      conclusion: routes.get('root_cause_analysis/conclusion'),
    });
  });

  it('un porqué de 40 caracteres y un Enter se envía sin el salto', async () => {
    const { loop } = await setup();
    await start();
    const forty = 'Porqué sintético de cuarenta caracteres.';
    expect(Array.from(forty)).toHaveLength(40);
    fireEvent.change(statement(1), { target: { value: `${forty}\n` } });
    expect(whyPart(1).querySelector('[data-part="line_break"]')).toBeNull();
    fireEvent.click(saveButton());
    await screen.findByText(
      text('scr11.step_saved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    expect(loop.calls.steps).toEqual([
      { index: 1, body: { statement: forty, evidence_refs: [], category: null } },
    ]);
  });

  it('con 14 caracteres el contador lo dice, el error sale al guardar y no hay petición', async () => {
    const { loop } = await setup();
    await start();
    fireEvent.change(statement(1), { target: { value: 'Catorce letras' } });
    expect(whyPart(1).querySelector('[data-part="counter"]')?.textContent).toBe(
      `14 / 1000 ${CHARACTERS} (${MINIMUM} 20)`,
    );
    fireEvent.click(saveButton());
    await waitFor(() => {
      expect(
        whyPart(1).querySelector('[data-part="local_error"]')?.getAttribute('data-text-key'),
      ).toBe('validation.free_text.too_short');
    }, WAIT);
    expect(loop.calls.steps).toEqual([]);
    expect((statement(1) as HTMLTextAreaElement).value).toBe('Catorce letras');
  });

  it('un salto en medio avisa en vivo, no envía y «Unir en una línea» deja un espacio', async () => {
    const { loop } = await setup();
    await start();
    fireEvent.change(statement(1), {
      target: { value: 'Primer porqué sintético\ncon un salto en medio' },
    });
    const warning = whyPart(1).querySelector('[data-part="line_break"]');
    expect(warning?.querySelector('[data-text-key]')?.getAttribute('data-text-key')).toBe(
      'validation.free_text.line_break',
    );
    fireEvent.click(saveButton());
    await act(async () => {
      await Promise.resolve();
    });
    expect(loop.calls.steps).toEqual([]);
    expect((statement(1) as HTMLTextAreaElement).value).toBe(
      'Primer porqué sintético\ncon un salto en medio',
    );
    fireEvent.click(
      within(whyPart(1)).getByRole('button', { name: text('components_forms.reason.join_lines') }),
    );
    expect((statement(1) as HTMLTextAreaElement).value).toBe(
      'Primer porqué sintético con un salto en medio',
    );
    fireEvent.click(saveButton());
    await screen.findByText(
      text('scr11.step_saved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    expect(loop.calls.steps.map((call) => call.body.statement)).toEqual([
      'Primer porqué sintético con un salto en medio',
    ]);
  });

  it('una atribución a persona muestra la sugerencia anclada al porqué y conserva lo escrito', async () => {
    const { loop } = await setup();
    await start();
    const suggestion = 'Sugerencia sintética del patrón 580';
    loop.state.stepResponse = () =>
      mswApiError('invalid_request', 400, {
        detailCode: 'loop_person_attribution_detected',
        rejection: { code: 'person_attribution_detected', suggestion },
      });
    const written = 'Porqué sintético que el validador rechaza.';
    fireEvent.change(statement(1), { target: { value: written } });
    fireEvent.click(saveButton());
    await waitFor(() => {
      expect(anchoredTo(statement(1))).not.toBeNull();
    }, WAIT);
    const alert = anchoredTo(statement(1));
    expect(alert?.querySelector('[data-part="message"]')?.textContent).toBe(suggestion);
    expect(alert?.querySelector('[data-part="rejection-suggestion"]')?.textContent).toBe(
      suggestion,
    );
    expect((statement(1) as HTMLTextAreaElement).value).toBe(written);
  });

  it('un 400 sin detail_code dice qué revisar, con el correlation_id, nunca el genérico', async () => {
    const { loop } = await setup();
    await start();
    const response = apiError('invalid_request', 400);
    const body = (await response.clone().json()) as { correlation_id: string };
    loop.state.stepResponse = () => response.clone();
    const written = 'Porqué sintético que el servidor rechaza.';
    fireEvent.change(statement(1), { target: { value: written } });
    fireEvent.click(saveButton());
    await waitFor(() => {
      expect(anchoredTo(statement(1))).not.toBeNull();
    }, WAIT);
    const alert = anchoredTo(statement(1));
    expect(alert?.getAttribute('data-text-key')).toBe('components_forms.reason.review');
    expect(alert?.querySelector('[data-part="message"]')?.textContent).toBe(
      text('components_forms.reason.review'),
    );
    expect(alert?.querySelector('[data-part="correlation_id"]')?.textContent).toBe(
      body.correlation_id,
    );
    expect((statement(1) as HTMLTextAreaElement).value).toBe(written);
  });

  it('un 400 sin detail_code al firmar dice qué revisar junto a la firma, con el correlation_id', async () => {
    const { loop } = await setup();
    await start();
    const block = await toSummary();
    const response = apiError('invalid_request', 400);
    const body = (await response.clone().json()) as { correlation_id: string };
    loop.state.signatureResponse = () => response.clone();
    fireEvent.click(
      within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    const alert = await waitFor(() => {
      const found = document.getElementById('signature-text');
      if (found === null) throw new Error('Aún no hay aviso junto a la firma.');
      return found;
    }, WAIT);
    expect(alert.getAttribute('data-text-key')).toBe('components_forms.reason.review');
    expect(alert.querySelector('[data-part="correlation_id"]')?.textContent).toBe(
      body.correlation_id,
    );
    expect(document.querySelectorAll('[role="alert"][data-code="invalid_request"]')).toHaveLength(
      1,
    );
  });

  it('una causa raíz con un salto en medio no firma; con un Enter final firma recortada', async () => {
    const { loop } = await setup();
    await start();
    await saveWhy(1, 'Primer porqué sintético con más de veinte caracteres.');
    next();
    for (let index = 2; index <= 5; index += 1) next();
    fireEvent.change(screen.getByRole('combobox', { name: text('scr11.root_category') }), {
      target: { value: 'engineering' },
    });
    const statementField = screen.getByRole('textbox', { name: rootName('scr11.root_statement') });
    fireEvent.change(statementField, {
      target: { value: 'Enunciado sintético\nde la causa raíz de ingeniería.' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: rootName('scr11.conclusion') }), {
      target: { value: 'Conclusión sintética del análisis de la zona.\n' },
    });
    next();
    const block = await screen.findByRole(
      'region',
      { name: text('components_forms.signature.title') },
      WAIT,
    );
    fireEvent.click(
      within(block).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    await screen.findByText(
      text('components_forms.reason.fix_before_send'),
      { selector: '[data-part="local_error"]' },
      WAIT,
    );
    expect(loop.calls.signatures).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.back') }));
    fireEvent.click(
      screen.getByRole('button', { name: text('components_forms.reason.join_lines') }),
    );
    next();
    const again = await screen.findByRole(
      'region',
      { name: text('components_forms.signature.title') },
      WAIT,
    );
    fireEvent.click(
      within(again).getByRole('button', { name: text('components_forms.signature.sign') }),
    );
    await screen.findByRole('region', { name: text('scr11.signed_title') }, WAIT);
    expect(loop.calls.signatures).toEqual([
      {
        professional_license: LICENSE,
        root_cause_category: 'engineering',
        root_cause_statement: 'Enunciado sintético de la causa raíz de ingeniería.',
        conclusion: 'Conclusión sintética del análisis de la zona.',
        cause_effect_branches: [],
      },
    ]);
  });
});
