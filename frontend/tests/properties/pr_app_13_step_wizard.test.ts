/**
 * PR-APP-13 (BR-APP-45): para todo flujo de 3 a 8 pasos y toda secuencia de acciones, el paso
 * `n + 1` solo es alcanzable con el paso `n` completo, «enviar» solo es alcanzable desde el
 * resumen y, sin `server_progress_ref`, no existe «guardar sin terminar».
 *
 * Pasos dinámicos (TASK-581): con `reshape` la lista cambia y la propiedad sigue valiendo; cada
 * paso conserva su «completo» por identidad y uno nuevo llega incompleto. En SCR-11, para toda
 * secuencia de ediciones, guardados y movimientos, la barra solo muestra los porqués guardados más
 * el siguiente y un porqué con texto solo está completo si está guardado y sincronizado.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { submittedText } from '@/components/ReasonField/ReasonField';
import {
  matches,
  savedStep,
  visibleWhyCount,
  WHY_COUNT,
  whyComplete,
  whyHasChanges,
  type WhyDraft,
} from '@/pages/scr11/root_cause/whySteps';
import type { Analysis } from '@/ports/scr11RootCause';

import {
  canAdvance,
  canGoTo,
  canSavePartial,
  canSubmit,
  createStepWizard,
  isSummary,
  stepWizardReducer,
  type StepWizardEvent,
  type StepWizardState,
} from '@/state/reducers/stepWizard';

import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const index = fc.integer({ min: -2, max: 10 });
const ref = fc.constantFrom('rca-0001', 'walk-test-7', '', '   ');

/** `complete_current` completa el paso en pantalla, para que los recorridos lleguen lejos. */
type Action = StepWizardEvent | { readonly type: 'complete_current' };

const event: fc.Arbitrary<Action> = fc.oneof(
  { weight: 6, arbitrary: fc.constant({ type: 'complete_current' } as const) },
  { weight: 2, arbitrary: index.map((i) => ({ type: 'complete_step', index: i }) as const) },
  { weight: 1, arbitrary: index.map((i) => ({ type: 'invalidate_step', index: i }) as const) },
  { weight: 6, arbitrary: fc.constant({ type: 'next' } as const) },
  { weight: 1, arbitrary: fc.constant({ type: 'back' } as const) },
  { weight: 2, arbitrary: index.map((i) => ({ type: 'go_to', index: i }) as const) },
  { weight: 3, arbitrary: fc.constant({ type: 'submit' } as const) },
  { weight: 2, arbitrary: fc.constant({ type: 'save_partial' } as const) },
  { weight: 1, arbitrary: ref.map((r) => ({ type: 'server_progress', ref: r }) as const) },
  {
    weight: 1,
    arbitrary: fc
      .tuple(ref, index)
      .map(([r, n]) => ({ type: 'resume', ref: r, completedSteps: n }) as const),
  },
  {
    weight: 2,
    arbitrary: fc.constantFrom(true, true, false).map((ok) => ({ type: 'settled', ok }) as const),
  },
  {
    weight: 2,
    arbitrary: fc
      .array(fc.option(index, { nil: null }), { minLength: 0, maxLength: 8 })
      .map((from) => ({ type: 'reshape', from }) as const),
  },
);

const flow = fc.record({
  steps: fc.integer({ min: 3, max: 8 }),
  initialRef: fc.option(ref, { nil: null }),
  // `size: 'max'`: con el tamaño por omisión las secuencias rondan 10 acciones y no cubren
  // flujos de 8 pasos con resumen y envío.
  events: fc.array(event, { maxLength: 80, size: 'max' }),
});

function allCompleteBefore(state: StepWizardState, n: number): boolean {
  return state.completed.slice(0, n).every(Boolean);
}

function checkInvariants(state: StepWizardState): void {
  expect(state.position).toBeGreaterThanOrEqual(0);
  expect(state.position).toBeLessThanOrEqual(state.stepCount);
  expect(state.completed).toHaveLength(state.stepCount);
  // Ningún paso es alcanzable con uno anterior incompleto.
  expect(allCompleteBefore(state, state.position)).toBe(true);
  if (state.serverProgressRef === null) expect(canSavePartial(state)).toBe(false);
  if (canSubmit(state)) expect(isSummary(state)).toBe(true);
}

describe('PR-APP-13 · flujo por pasos', () => {
  it(
    'el paso n+1 solo con el n completo; enviar solo desde el resumen; guardar solo con referencia',
    () => {
      let reachedSummary = 0;
      let submittedRuns = 0;
      fc.assert(
        fc.property(flow, ({ steps, initialRef, events }) => {
          let state = createStepWizard(steps, initialRef);
          checkInvariants(state);
          for (const e of events) {
            const applied: StepWizardEvent =
              e.type === 'complete_current' ? { type: 'complete_step', index: state.position } : e;
            const before = state;
            state = stepWizardReducer(before, applied);
            checkInvariants(state);
            if (applied.type === 'reshape') {
              // Cada paso conserva su «completo» por identidad; uno nuevo llega incompleto.
              if (applied.from.length === 0) {
                expect(state).toBe(before);
              } else {
                expect(state.stepCount).toBe(applied.from.length);
                // El paso en pantalla se sigue por identidad, y el resumen sigue siendo el resumen,
                // mientras ningún paso anterior quede incompleto.
                const kept = applied.from.indexOf(before.position);
                if (isSummary(before) && state.completed.every(Boolean)) {
                  expect(isSummary(state)).toBe(true);
                } else if (!isSummary(before) && kept >= 0 && allCompleteBefore(state, kept)) {
                  expect(state.position).toBe(kept);
                }
                state.completed.forEach((done, i) => {
                  const old = applied.from[i];
                  const known = old != null && old >= 0 && old < before.stepCount;
                  expect(done).toBe(known && before.completed[old] === true);
                });
              }
            }
            if (
              state.position > before.position &&
              applied.type !== 'resume' &&
              applied.type !== 'reshape'
            ) {
              // Solo un paso por vez hacia delante con `next`; con `go_to`, todos los anteriores.
              for (let k = before.position; k < state.position; k++) {
                expect(before.completed[k]).toBe(true);
              }
              if (applied.type === 'next') expect(state.position).toBe(before.position + 1);
            }
            if (before.pending === null && state.pending === 'submit') {
              expect(isSummary(before)).toBe(true);
              expect(allCompleteBefore(before, before.stepCount)).toBe(true);
            }
            if (before.pending === null && state.pending === 'save_partial') {
              expect(before.serverProgressRef).not.toBeNull();
            }
            if (applied.type === 'save_partial' && before.serverProgressRef === null) {
              expect(state).toBe(before);
            }
            if (state.submitted) expect(before.pending === 'submit' || before.submitted).toBe(true);
          }
          if (isSummary(state)) reachedSummary++;
          if (state.submitted) submittedRuns++;
        }),
      );
      // La propiedad no es vacía: hay recorridos que llegan al resumen y envían.
      expect(reachedSummary).toBeGreaterThan(0);
      expect(submittedRuns).toBeGreaterThan(0);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'avanzar sin completar el paso actual no mueve el flujo',
    () => {
      fc.assert(
        fc.property(fc.integer({ min: 3, max: 8 }), (steps) => {
          const state = createStepWizard(steps);
          expect(canAdvance(state)).toBe(false);
          expect(canGoTo(state, 0)).toBe(true);
          for (let target = 1; target <= steps; target++) {
            expect(canGoTo(state, target)).toBe(false);
          }
          expect(stepWizardReducer(state, { type: 'next' })).toBe(state);
          expect(stepWizardReducer(state, { type: 'go_to', index: 1 })).toBe(state);
          expect(stepWizardReducer(state, { type: 'go_to', index: steps })).toBe(state);
          expect(stepWizardReducer(state, { type: 'submit' })).toBe(state);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'recorrido completo: cada paso, resumen, enviar y confirmación',
    () => {
      fc.assert(
        fc.property(fc.integer({ min: 3, max: 8 }), (steps) => {
          let state = createStepWizard(steps);
          for (let n = 0; n < steps; n++) {
            expect(isSummary(state)).toBe(false);
            state = stepWizardReducer(state, { type: 'complete_step', index: n });
            state = stepWizardReducer(state, { type: 'next' });
          }
          expect(isSummary(state)).toBe(true);
          expect(canSavePartial(state)).toBe(false);
          state = stepWizardReducer(state, { type: 'submit' });
          expect(state.pending).toBe('submit');
          // Pendiente: el flujo no cambia hasta el resultado.
          expect(stepWizardReducer(state, { type: 'back' })).toBe(state);
          const failed = stepWizardReducer(state, { type: 'settled', ok: false });
          expect(failed.submitted).toBe(false);
          expect(canSubmit(failed)).toBe(true);
          state = stepWizardReducer(state, { type: 'settled', ok: true });
          expect(state.submitted).toBe(true);
          expect(canSubmit(state)).toBe(false);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('guardar sin terminar y reanudar desde el servidor', () => {
    let state = createStepWizard(5);
    expect(canSavePartial(state)).toBe(false);
    state = stepWizardReducer(state, { type: 'server_progress', ref: '  ' });
    expect(canSavePartial(state)).toBe(false);
    state = stepWizardReducer(state, { type: 'server_progress', ref: 'rca-0001' });
    expect(canSavePartial(state)).toBe(true);
    state = stepWizardReducer(state, { type: 'save_partial' });
    expect(state.pending).toBe('save_partial');
    state = stepWizardReducer(state, { type: 'settled', ok: true });
    expect(state.submitted).toBe(false);
    expect(state.pending).toBeNull();

    const resumed = stepWizardReducer(createStepWizard(5), {
      type: 'resume',
      ref: 'rca-0001',
      completedSteps: 3,
    });
    expect(resumed.position).toBe(3);
    expect(resumed.completed).toEqual([true, true, true, false, false]);
    expect(canSavePartial(resumed)).toBe(true);
    const beyond = stepWizardReducer(createStepWizard(3), {
      type: 'resume',
      ref: 'rca-0001',
      completedSteps: 99,
    });
    expect(isSummary(beyond)).toBe(true);
  });

  it('rechaza flujos sin pasos', () => {
    expect(() => createStepWizard(0)).toThrow(RangeError);
    expect(() => createStepWizard(2.5)).toThrow(RangeError);
    expect(createStepWizard(3, '').serverProgressRef).toBeNull();
  });
});

// --- SCR-11: los porqués como pasos dinámicos (TASK-581) ----------------------------------------

const ZONE = '0190a1b2-1111-7000-8000-000000000201';

function emptyAnalysis(): Analysis {
  return {
    analysis_id: '0190a1b2-4444-7000-8000-000000000201',
    subject_kind: 'finding',
    subject_id: '0190a1b2-2222-7000-8000-000000000201',
    zone_id: ZONE,
    state: 'draft',
    zone_context: {
      zone_id: ZONE,
      family: 'coexistence',
      tier: 'tier_1',
      standard_id: '0190a1b2-3333-7000-8000-000000000201',
      standard_version: 1,
      standard_title_es: 'Estándar sintético',
      standard_text: 'Texto sintético del estándar.',
      observability_state_at_event: 'observable',
      period: { from: '2026-02-01T00:00:00.000Z', to: '2026-03-02T12:00:00.000Z' },
      coverage_summary_period: {
        observable_ms: 0,
        degraded_ms: 0,
        not_observable_ms: 0,
        no_communication_ms: 0,
        never_reported_ms: 0,
        zone_not_active_ms: 0,
      },
      node_state: [],
      signals_by_role: [],
      episode_durations: { episode_ms: null, condition_ms: null },
      finding_counts_by_family_in_period: [],
      recurrences_in_period: 0,
      previous_actions: [],
      previous_cause_categories: [],
    },
    why_steps: [],
    next_step_index: 1,
    next_question_es: 'Pregunta sintética 1',
    cause_effect_branches: [],
    root_cause_category: null,
    root_cause_statement: null,
    conclusion: null,
    pending_information: ['why_steps', 'root_cause_category', 'root_cause_statement', 'conclusion'],
    missing_information: null,
    record_id: null,
    signer: null,
    signed_at: null,
    created_at: '2026-03-02T12:00:00.000Z',
    updated_at: '2026-03-02T12:00:00.000Z',
  };
}

const STATEMENT = fc.constantFrom(
  '',
  '   ',
  'Porqué sintético alfa con más de veinte caracteres',
  'Porqué sintético alfa con más de veinte caracteres\n',
  '  Porqué sintético alfa con más de veinte caracteres ',
  'Porqué sintético beta con más de veinte caracteres',
);
const CATEGORY = fc.constantFrom('', 'process', 'engineering', 'organization');

/** Una acción de la persona sobre SCR-11: editar el paso en pantalla, guardarlo o moverse. */
type WhyAction =
  | { readonly type: 'edit'; readonly statement: string; readonly category: string }
  | { readonly type: 'save' }
  | { readonly type: 'next' }
  | { readonly type: 'back' }
  | { readonly type: 'go_to'; readonly index: number };

const whyAction: fc.Arbitrary<WhyAction> = fc.oneof(
  {
    weight: 4,
    arbitrary: fc
      .tuple(STATEMENT, CATEGORY)
      .map(([statement, category]) => ({ type: 'edit', statement, category }) as const),
  },
  { weight: 3, arbitrary: fc.constant({ type: 'save' } as const) },
  { weight: 4, arbitrary: fc.constant({ type: 'next' } as const) },
  { weight: 1, arbitrary: fc.constant({ type: 'back' } as const) },
  {
    weight: 2,
    arbitrary: fc.integer({ min: -1, max: 8 }).map((i) => ({ type: 'go_to', index: i }) as const),
  },
);

/** El servidor de U-04: admite el siguiente porqué o uno guardado, con 20 a 1000 caracteres. */
function serverPut(analysis: Analysis, index: number, draft: WhyDraft): Analysis | null {
  const statement = submittedText(draft.statement);
  if (index < 1 || index > Math.min(analysis.why_steps.length + 1, WHY_COUNT)) return null;
  if (Array.from(statement).length < 20 || /[\u0000-\u001f]/u.test(statement)) return null;
  const step = {
    step_index: index,
    question_es: `Pregunta sintética ${String(index)}`,
    statement,
    evidence_refs: [],
    category: draft.category === '' ? null : draft.category,
  };
  const why_steps = [...analysis.why_steps.filter((s) => s.step_index !== index), step].sort(
    (a, b) => a.step_index - b.step_index,
  );
  return { ...analysis, why_steps };
}

/** Los pasos que SCR-11 da a UI-15: los porqués visibles y la causa raíz. */
function screenSteps(analysis: Analysis, drafts: readonly WhyDraft[], rootComplete: boolean) {
  const whys = Array.from({ length: visibleWhyCount(analysis) }, (_, offset) => ({
    id: `why-${String(offset + 1)}`,
    complete: whyComplete(analysis, drafts, offset),
  }));
  return [...whys, { id: 'root', complete: rootComplete }];
}

describe('PR-APP-13 · SCR-11 con los porqués guardados más el siguiente', () => {
  it(
    'el paso n+1 solo con el n completo, y un porqué con texto solo completo si está guardado y sincronizado',
    () => {
      let reachedRoot = 0;
      let savedThree = 0;
      fc.assert(
        fc.property(
          fc.array(whyAction, { maxLength: 60, size: 'max' }),
          fc.boolean(),
          (actions, rootComplete) => {
            let analysis = emptyAnalysis();
            let drafts: WhyDraft[] = Array.from({ length: WHY_COUNT }, () => ({
              statement: '',
              category: '',
            }));
            let steps = screenSteps(analysis, drafts, rootComplete);
            let state = createStepWizard(steps.length, analysis.analysis_id);
            /** Como UI-15: sigue la lista por `id` y marca solo el paso en pantalla. */
            const sync = () => {
              const next = screenSteps(analysis, drafts, rootComplete);
              const before = steps.map((step) => step.id);
              if (before.join() !== next.map((step) => step.id).join()) {
                state = stepWizardReducer(state, {
                  type: 'reshape',
                  from: next.map((step) => {
                    const i = before.indexOf(step.id);
                    return i >= 0 ? i : null;
                  }),
                });
              }
              steps = next;
              const current = steps[state.position];
              if (current !== undefined) {
                state = stepWizardReducer(state, {
                  type: current.complete ? 'complete_step' : 'invalidate_step',
                  index: state.position,
                });
              }
            };
            sync();
            for (const action of actions) {
              const offset = state.position;
              const onWhy = steps[offset]?.id.startsWith('why-') === true;
              if (action.type === 'edit' && onWhy) {
                drafts = drafts.map((d, i) =>
                  i === offset ? { statement: action.statement, category: action.category } : d,
                );
              } else if (action.type === 'save' && onWhy) {
                const draft = drafts[offset];
                if (draft !== undefined && whyHasChanges(analysis, drafts, offset)) {
                  analysis = serverPut(analysis, offset + 1, draft) ?? analysis;
                }
              } else if (action.type === 'next' || action.type === 'back') {
                state = stepWizardReducer(state, { type: action.type });
              } else if (action.type === 'go_to') {
                state = stepWizardReducer(state, { type: 'go_to', index: action.index });
              }
              sync();

              // La barra: los guardados más el siguiente, nunca más de cinco ni uno fuera de orden.
              const whyIds = steps.filter((step) => step.id.startsWith('why-')).map((s) => s.id);
              expect(whyIds).toEqual(
                Array.from(
                  { length: Math.min(analysis.why_steps.length + 1, WHY_COUNT) },
                  (_, i) => `why-${String(i + 1)}`,
                ),
              );
              // Un porqué con texto solo cuenta como completo si está guardado y sincronizado.
              whyIds.forEach((_, i) => {
                const draft = drafts[i];
                if (draft === undefined || draft.statement.trim() === '') return;
                if (whyComplete(analysis, drafts, i)) {
                  expect(matches(savedStep(analysis, i + 1), draft)).toBe(true);
                }
              });
              // Todo paso anterior al de pantalla está completo con lo que hay ahora.
              for (let k = 0; k < Math.min(state.position, steps.length); k++) {
                expect(steps[k]?.complete).toBe(true);
              }
              if (steps[state.position]?.id === 'root') reachedRoot++;
            }
            if (analysis.why_steps.length >= 3) savedThree++;
          },
        ),
      );
      // La propiedad no es vacía: hay recorridos que llegan a la causa raíz y guardan tres porqués.
      expect(reachedRoot).toBeGreaterThan(0);
      expect(savedThree).toBeGreaterThan(0);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('ejemplos: en blanco el siguiente cierra la cadena; con texto sin guardar, no', () => {
    const saved = serverPut(emptyAnalysis(), 1, {
      statement: 'Porqué sintético alfa con más de veinte caracteres',
      category: '',
    });
    expect(saved).not.toBeNull();
    if (saved === null) return;
    const first: WhyDraft = {
      statement: 'Porqué sintético alfa con más de veinte caracteres',
      category: '',
    };
    const blank: WhyDraft[] = [first, { statement: '', category: '' }];
    expect(visibleWhyCount(saved)).toBe(2);
    expect(whyComplete(saved, blank, 1)).toBe(true);
    const written: WhyDraft[] = [first, { statement: 'Texto sin guardar', category: '' }];
    expect(whyComplete(saved, written, 1)).toBe(false);
    const recategorized: WhyDraft[] = [
      { statement: 'Porqué sintético alfa con más de veinte caracteres', category: 'engineering' },
    ];
    expect(whyComplete(saved, recategorized, 0)).toBe(false);
    expect(whyHasChanges(saved, recategorized, 0)).toBe(true);
    expect(visibleWhyCount(emptyAnalysis())).toBe(1);
  });
});
