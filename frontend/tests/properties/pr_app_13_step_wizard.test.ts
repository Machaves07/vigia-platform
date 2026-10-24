/**
 * PR-APP-13 (BR-APP-45): para todo flujo de 3 a 8 pasos y toda secuencia de acciones, el paso
 * `n + 1` solo es alcanzable con el paso `n` completo, «enviar» solo es alcanzable desde el
 * resumen y, sin `server_progress_ref`, no existe «guardar sin terminar».
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

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
            if (state.position > before.position && applied.type !== 'resume') {
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
