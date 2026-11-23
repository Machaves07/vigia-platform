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
import userEvent from '@testing-library/user-event';
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

/** Nombre accesible del porqué: obligatorio el primero y todo porqué ya guardado. */
function statementName(index: number, requirement: 'required' | 'optional'): string {
  return `${text('scr11.statement')} ${String(index)} (${text(`components_forms.reason.${requirement}`)})`;
}

function statement(index: number) {
  return (
    screen.queryByRole('textbox', { name: statementName(index, 'required') }) ??
    screen.getByRole('textbox', { name: statementName(index, 'optional') })
  );
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
  // El porqué 2 en blanco cierra la cadena: «Siguiente» lleva a la causa raíz (TASK-581).
  next();
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

/** La barra de pasos de UI-15. */
function bar(): HTMLElement {
  return screen.getByRole('list', { name: text('components_structure.wizard.steps_label') });
}

function barItems(): HTMLElement[] {
  return [...bar().querySelectorAll<HTMLElement>(':scope > li')];
}

/** El rótulo de un paso de la barra, sin su separador (que es `aria-hidden`). */
function itemLabel(item: HTMLElement): string {
  return [...item.childNodes]
    .filter((node) => !(node instanceof HTMLElement && node.dataset.part === 'step-separator'))
    .map((node) => node.textContent)
    .join('');
}

function barLabels(): string[] {
  return barItems().map(itemLabel);
}

/** Los pasos a los que la barra deja ir: los que son botón. */
function barLinks(): string[] {
  return barItems()
    .filter((item) => item.querySelector(':scope > button') !== null)
    .map(itemLabel);
}

/** «Paso x de n» de UI-15, por sus partes. */
function progress(): { current: number; total: number } | null {
  const title = document.querySelector('[data-part="step-title"]');
  const current = title?.querySelector('[data-part="progress-current"]')?.textContent;
  const total = title?.querySelector('[data-part="progress-total"]')?.textContent;
  if (current == null || total == null) return null;
  return { current: Number(current), total: Number(total) };
}

function whyLabel(index: number): string {
  return `${text('scr11.why_title')} ${String(index)}`;
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
    expect(advanceButton()).toHaveProperty('disabled', true);
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético de la zona.' } });
    // Sin guardar, la barra no deja ir a ningún paso posterior (TASK-581).
    expect(barLinks()).toEqual([]);
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

  it.each([
    ['409', 'conflict', 409],
    ['400', 'invalid_request', 400],
  ] as const)(
    'un %s loop_analysis_step_out_of_order queda anclado al porqué con su etiqueta y conserva lo escrito',
    async (_status, code, status) => {
      const { loop } = await setup();
      await start();
      await saveWhy(1, 'Primer porqué sintético de la zona.');
      next();
      // El servidor decide el orden (BR-LAZ-61): otra pestaña pudo cambiar el borrador.
      loop.state.stepResponse = () => apiError(code, status, 'loop_analysis_step_out_of_order');
      fireEvent.change(statement(2), { target: { value: 'Segundo porqué sintético de la zona.' } });
      fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
      await waitFor(() => {
        expect(anchoredTo(statement(2))).not.toBeNull();
      }, WAIT);
      const anchored = anchoredTo(statement(2));
      expect(anchored?.querySelector('[data-part="message"]')?.textContent).toBe(
        detail.loop_analysis_step_out_of_order,
      );
      expect((statement(2) as HTMLTextAreaElement).value).toBe(
        'Segundo porqué sintético de la zona.',
      );
      expect(loop.calls.steps.map((call) => call.index)).toEqual([1, 2]);
      expect(progress()).toEqual({ current: 2, total: 3 });
    },
  );

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
    next();
    next();
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

  it('un porqué guardado no se puede vaciar: en blanco es «obligatorio» y no hay petición', async () => {
    const { loop } = await setup();
    await start();
    await saveWhy(1, 'Primer porqué sintético con más de veinte caracteres.');
    next();
    // Sin guardar, el porqué 2 es opcional; guardado, pasa a obligatorio.
    expect(screen.queryByRole('textbox', { name: statementName(2, 'optional') })).not.toBeNull();
    await saveWhy(2, 'Segundo porqué sintético con más de veinte caracteres.');
    expect(screen.queryByRole('textbox', { name: statementName(2, 'required') })).not.toBeNull();
    const before = loop.calls.steps.length;
    fireEvent.change(statement(2), { target: { value: '' } });
    fireEvent.click(saveButton());
    await waitFor(() => {
      expect(
        whyPart(2).querySelector('[data-part="local_error"]')?.getAttribute('data-text-key'),
      ).toBe('validation.required');
    }, WAIT);
    await act(async () => {
      await new Promise((done) => {
        setTimeout(done, 50);
      });
    });
    expect(loop.calls.steps).toHaveLength(before);
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
    next();
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

// --- TASK-581 (O-9, O-5): causa raíz que se puede completar ------------------------------------

describe('SCR-11 TASK-581: causa raíz que se puede completar', { timeout: 60_000 }, () => {
  const SAVED = [
    'Primer porqué sintético guardado en el servidor.',
    'Segundo porqué sintético guardado en el servidor.',
  ];
  /** El texto del porqué 3 del recorrido del 22-11 (O-8): minúsculas, sin datos de nadie. */
  const THIRD = 'causa raíz numero 3 porque esta muy bien definida y no tiene causa';

  function twoSaved() {
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

  /** Retoma un análisis con dos porqués guardados y entra al flujo. */
  async function resumedWithTwo() {
    const env = await providers();
    const loop = fakeLoop();
    loop.saved(twoSaved());
    loop.install(env.clock);
    const view = renderAt(<Page />, env.Providers);
    fireEvent.click(await screen.findByRole('button', { name: text('scr11.continue') }, WAIT));
    await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
    return { ...env, loop, view };
  }

  function title(): string | undefined {
    const heading = document.querySelector('[data-part="step-title"]');
    return heading?.textContent.split(': ').slice(1).join(': ');
  }

  async function settle() {
    await act(async () => {
      await new Promise((done) => {
        setTimeout(done, 50);
      });
    });
  }

  it('la barra muestra los porqués guardados más el siguiente, cada uno en su elemento con un separador', async () => {
    await resumedWithTwo();
    const root = text('scr11.root_title');
    const summary = text('components_structure.wizard.summary');
    expect(barLabels()).toEqual([whyLabel(1), whyLabel(2), whyLabel(3), root, summary]);
    const items = barItems();
    expect(items.map((item) => item.tagName)).toEqual(['LI', 'LI', 'LI', 'LI', 'LI']);
    // Un separador entre pasos: ninguno antes del primero, uno en cada uno de los demás.
    expect(
      items.map((item) => item.querySelectorAll(':scope > [data-part="step-separator"]').length),
    ).toEqual([0, 1, 1, 1, 1]);
    for (const separator of bar().querySelectorAll('[data-part="step-separator"]')) {
      expect(separator.getAttribute('aria-hidden')).toBe('true');
    }
    expect(progress()).toEqual({ current: 1, total: 4 });
    expect(title()).toBe(whyLabel(1));
  });

  it('con 2 porqués guardados y el 3 en blanco, «Siguiente» lleva a la causa raíz sin ninguna petición', async () => {
    const { loop } = await resumedWithTwo();
    next();
    next();
    expect(progress()).toEqual({ current: 3, total: 4 });
    expect(title()).toBe(whyLabel(3));
    expect(
      whyPart(3).querySelector('[data-part="optional-hint"]')?.getAttribute('data-text-key'),
    ).toBe('scr11.optional_step');
    expect(text('scr11.optional_step')).toBe(
      'Si la causa ya está identificada, deje este porqué en blanco y pase a “Causa raíz y conclusión”.',
    );
    next();
    expect(title()).toBe(text('scr11.root_title'));
    expect(progress()).toEqual({ current: 4, total: 4 });
    expect(document.querySelector('[data-part="root"]')).not.toBeNull();
    expect(barLabels()).toEqual([
      whyLabel(1),
      whyLabel(2),
      whyLabel(3),
      text('scr11.root_title'),
      text('components_structure.wizard.summary'),
    ]);
    await settle();
    expect(loop.calls.steps).toEqual([]);
  });

  it('un porqué guardado no lleva la pista de dejarlo en blanco; el siguiente sí', async () => {
    await resumedWithTwo();
    next();
    expect(whyPart(2).querySelector('[data-part="optional-hint"]')).toBeNull();
    next();
    expect(whyPart(3).querySelector('[data-part="optional-hint"]')).not.toBeNull();
  });

  it('un porqué con texto sin guardar no deja ir a los pasos siguientes; guardado, aparece el siguiente', async () => {
    await setup();
    await start();
    expect(barLabels()).toEqual([
      whyLabel(1),
      text('scr11.root_title'),
      text('components_structure.wizard.summary'),
    ]);
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético sin guardar.' } });
    expect(barLinks()).toEqual([]);
    await saveWhy(1, 'Primer porqué sintético ya guardado aquí.');
    expect(barLabels()).toEqual([
      whyLabel(1),
      whyLabel(2),
      text('scr11.root_title'),
      text('components_structure.wizard.summary'),
    ]);
    expect(progress()).toEqual({ current: 1, total: 3 });
    expect(barLinks()).toEqual([whyLabel(2)]);
    next();
    fireEvent.change(statement(2), { target: { value: 'Segundo porqué sintético sin guardar.' } });
    // Con el 2 escrito y sin guardar, ni la causa raíz ni el resumen son alcanzables.
    expect(barLinks()).toEqual([whyLabel(1)]);
    // Y un porqué guardado que se edita deja de estar completo hasta guardarlo otra vez.
    fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.back') }));
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético editado aquí.' } });
    expect(barLinks()).toEqual([]);
  });

  it('«Siguiente» con un porqué escrito lo guarda con su categoría y avanza al siguiente', async () => {
    const { loop } = await setup();
    await start();
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético de la zona.' } });
    fireEvent.change(screen.getByRole('combobox', { name: text('scr11.category') }), {
      target: { value: 'process' },
    });
    next();
    await waitFor(() => {
      expect(title()).toBe(whyLabel(2));
    }, WAIT);
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
    expect(progress()).toEqual({ current: 2, total: 3 });
    // Volver al porqué guardado no lo vuelve a guardar ni avanza solo.
    fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.back') }));
    await settle();
    expect(title()).toBe(whyLabel(1));
    expect(loop.calls.steps).toHaveLength(1);
  });

  it('«Siguiente» con un error local no envía ni avanza, y el error queda en el campo', async () => {
    const { loop } = await setup();
    await start();
    fireEvent.change(statement(1), { target: { value: 'Catorce letras' } });
    next();
    await waitFor(() => {
      expect(
        whyPart(1).querySelector('[data-part="local_error"]')?.getAttribute('data-text-key'),
      ).toBe('validation.free_text.too_short');
    }, WAIT);
    await settle();
    expect(loop.calls.steps).toEqual([]);
    expect(title()).toBe(whyLabel(1));
  });

  it('«Siguiente» con un rechazo del servidor no avanza y el aviso queda anclado', async () => {
    const { loop } = await setup();
    await start();
    loop.state.stepResponse = () => apiError('invalid_request', 400);
    fireEvent.change(statement(1), {
      target: { value: 'Porqué sintético que el servidor rechaza.' },
    });
    next();
    await waitFor(() => {
      expect(anchoredTo(statement(1))).not.toBeNull();
    }, WAIT);
    await settle();
    expect(title()).toBe(whyLabel(1));
    expect(loop.calls.steps.map((call) => call.index)).toEqual([1]);
  });

  it('«Siguiente» mientras guarda no envía dos veces; si se edita durante el guardado, no avanza', async () => {
    const { loop } = await setup();
    await start();
    let release: (value: null) => void = () => undefined;
    loop.state.stepResponse = () =>
      new Promise<null>((done) => {
        release = done;
      });
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético de la zona.' } });
    next();
    await waitFor(() => {
      expect(loop.calls.steps).toHaveLength(1);
    }, WAIT);
    // Guardando: «Siguiente» no vuelve a enviar.
    next();
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético cambiado aquí.' } });
    loop.state.stepResponse = null;
    await act(async () => {
      release(null);
      await Promise.resolve();
    });
    await screen.findByText(
      text('scr11.step_unsaved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    await settle();
    expect(loop.calls.steps.map((call) => call.body.statement)).toEqual([
      'Primer porqué sintético de la zona.',
    ]);
    // Lo guardado no es lo que hay en pantalla: el flujo sigue en el porqué 1.
    expect(title()).toBe(whyLabel(1));
  });

  it('«Siguiente» con «Guardar porqué» en vuelo espera ese guardado: un solo PUT y avanza al resolver', async () => {
    const { loop } = await setup();
    await start();
    let release: (value: null) => void = () => undefined;
    loop.state.stepResponse = () =>
      new Promise<null>((done) => {
        release = done;
      });
    fireEvent.change(statement(1), { target: { value: 'Primer porqué sintético de la zona.' } });
    fireEvent.click(screen.getByRole('button', { name: text('scr11.save_step') }));
    await waitFor(() => {
      expect(loop.calls.steps).toHaveLength(1);
    }, WAIT);
    // Con el guardado en vuelo, «Siguiente» no envía otro PUT del mismo paso.
    next();
    await settle();
    expect(loop.calls.steps).toHaveLength(1);
    expect(title()).toBe(whyLabel(1));
    loop.state.stepResponse = null;
    await act(async () => {
      release(null);
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(title()).toBe(whyLabel(2));
    }, WAIT);
    await settle();
    expect(loop.calls.steps).toEqual([
      {
        index: 1,
        body: {
          statement: 'Primer porqué sintético de la zona.',
          evidence_refs: [],
          category: null,
        },
      },
    ]);
    expect(progress()).toEqual({ current: 2, total: 3 });
  });

  it('«Siguiente» con un porqué guardado y vaciado dice que es obligatorio y no envía', async () => {
    const { loop } = await resumedWithTwo();
    next();
    fireEvent.change(statement(2), { target: { value: '' } });
    next();
    await waitFor(() => {
      expect(
        whyPart(2).querySelector('[data-part="local_error"]')?.getAttribute('data-text-key'),
      ).toBe('validation.required');
    }, WAIT);
    await settle();
    expect(loop.calls.steps).toEqual([]);
    expect(title()).toBe(whyLabel(2));
  });

  it('elegir «Ingeniería» en el porqué 2 ya guardado lo marca sin guardar y «Siguiente» envía la categoría', async () => {
    const { loop } = await resumedWithTwo();
    next();
    expect(whyPart(2).querySelector('[data-part="step-sync"]')?.textContent).toBe(
      text('scr11.step_saved'),
    );
    fireEvent.change(within(whyPart(2)).getByRole('combobox', { name: text('scr11.category') }), {
      target: { value: 'engineering' },
    });
    expect(whyPart(2).querySelector('[data-part="step-sync"]')?.textContent).toBe(
      text('scr11.step_unsaved'),
    );
    next();
    await waitFor(() => {
      expect(title()).toBe(whyLabel(3));
    }, WAIT);
    expect(loop.calls.steps).toEqual([
      { index: 2, body: { statement: SAVED[1], evidence_refs: [], category: 'engineering' } },
    ]);
    expect(loop.analysis.why_steps.map((step) => [step.step_index, step.category])).toEqual([
      [1, null],
      [2, 'engineering'],
    ]);
    fireEvent.click(screen.getByRole('button', { name: text('components_structure.wizard.back') }));
    expect(
      within(whyPart(2)).getByRole<HTMLSelectElement>('combobox', { name: text('scr11.category') })
        .value,
    ).toBe('engineering');
    expect(whyPart(2).querySelector('[data-part="step-sync"]')?.textContent).toBe(
      text('scr11.step_saved'),
    );
  });

  it('«Guardar porqué» y «Guardar sin terminar» envían la categoría que se ve en pantalla', async () => {
    const { loop } = await resumedWithTwo();
    fireEvent.change(within(whyPart(1)).getByRole('combobox', { name: text('scr11.category') }), {
      target: { value: 'organization' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: text('components_structure.wizard.save_partial') }),
    );
    await screen.findByText(
      text('components_structure.wizard.saved_partial'),
      { selector: '[data-part="outcome"]' },
      WAIT,
    );
    next();
    fireEvent.change(within(whyPart(2)).getByRole('combobox', { name: text('scr11.category') }), {
      target: { value: 'process' },
    });
    fireEvent.click(saveButton());
    await screen.findByText(
      text('scr11.step_saved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    expect(loop.calls.steps).toEqual([
      { index: 1, body: { statement: SAVED[0], evidence_refs: [], category: 'organization' } },
      { index: 2, body: { statement: SAVED[1], evidence_refs: [], category: 'process' } },
    ]);
  });

  it('O-9: el porqué 3 escrito con un Enter final se envía sin el salto y se guarda (200)', async () => {
    const user = userEvent.setup();
    const { loop } = await resumedWithTwo();
    next();
    next();
    await user.type(statement(3), `${THIRD}{Enter}`);
    // Lo que tiene el campo es lo que el 22-11 la consola enviaba tal cual: el texto y un salto.
    expect((statement(3) as HTMLTextAreaElement).value).toBe(`${THIRD}\n`);
    await user.click(saveButton());
    await screen.findByText(
      text('scr11.step_saved'),
      { selector: '[data-part="step-sync"]' },
      WAIT,
    );
    expect(loop.calls.steps).toEqual([
      { index: 3, body: { statement: THIRD, evidence_refs: [], category: null } },
    ]);
    expect(loop.analysis.why_steps.map((step) => step.statement)).toEqual([...SAVED, THIRD]);
    expect(barLabels()).toEqual([
      whyLabel(1),
      whyLabel(2),
      whyLabel(3),
      whyLabel(4),
      text('scr11.root_title'),
      text('components_structure.wizard.summary'),
    ]);
  });

  it('con cinco porqués guardados no aparece un sexto', async () => {
    const env = await providers();
    const loop = fakeLoop();
    loop.saved(
      analysis({
        why_steps: [1, 2, 3, 4, 5].map((index) => ({
          step_index: index,
          question_es: question(index),
          statement: `Porqué sintético número ${String(index)} guardado.`,
          evidence_refs: [],
          category: null,
        })),
        next_step_index: null,
        next_question_es: null,
      }),
    );
    loop.install(env.clock);
    renderAt(<Page />, env.Providers);
    fireEvent.click(await screen.findByRole('button', { name: text('scr11.continue') }, WAIT));
    await screen.findByRole('region', { name: text('scr11.wizard_title') }, WAIT);
    expect(barLabels()).toEqual([
      whyLabel(1),
      whyLabel(2),
      whyLabel(3),
      whyLabel(4),
      whyLabel(5),
      text('scr11.root_title'),
      text('components_structure.wizard.summary'),
    ]);
  });

  it('axe sin violaciones con la barra, el indicador y el porqué siguiente', async () => {
    const { view } = await resumedWithTwo();
    next();
    next();
    expect(await axe(view.container)).toHaveNoViolations();
  });
});
