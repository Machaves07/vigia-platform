/**
 * Escenarios de PR-APP-14 sobre el controlador de UI-22 (`createAssistantPoller`): una respuesta
 * guionizada para la solicitud y para cada consulta, con latencias arbitrarias (también más allá
 * del tope o sin respuesta), y una secuencia de tics del `FakeClock`. `checkAssistantScenario`
 * ejecuta uno y comprueba el oráculo de BR-APP-49 con sus valores literales (5 000 ms y
 * 120 000 ms), nunca con las constantes del módulo bajo prueba (lección de VIG-292). Lo usan la
 * propiedad y sus regresiones.
 */
import fc from 'fast-check';
import { expect } from 'vitest';

import { createAssistantPoller } from '@/components/AssistantDraftPanel/poller';
import type {
  AssistantFailure,
  AssistantPanelState,
  AssistantRequestView,
} from '@/components/AssistantDraftPanel/state';

import { FakeClock } from './fakeClock';

type Kind =
  | 'pending'
  | 'ready'
  | 'unavailable'
  | 'rejected_by_validators'
  | 'unknown_state'
  | 'fail_unavailable'
  | 'fail_rejected'
  | 'fail_aborted';

interface Scripted {
  readonly kind: Kind;
  /** Milisegundos hasta la respuesta; `null`: no responde nunca (solo la cancela el tope). */
  readonly latency: number | null;
}

export interface AssistantScenario {
  readonly accept: { readonly kind: Kind; readonly latency: number };
  readonly responses: readonly Scripted[];
  readonly ticks: readonly number[];
}

/** Cómo terminó el escenario, para comprobar que la propiedad recorre todas las ramas. */
export type ScenarioOutcome = 'not_accepted' | 'terminal' | 'expired';

const HORIZON_MS = 200_000;

const TERMINAL: ReadonlySet<Kind> = new Set([
  'ready',
  'unavailable',
  'rejected_by_validators',
  'unknown_state',
  'fail_unavailable',
  'fail_rejected',
]);

/** Estado del panel al que lleva cada respuesta final. */
const EXPECTED_STATUS: Readonly<Partial<Record<Kind, AssistantPanelState['status']>>> = {
  ready: 'ready',
  unavailable: 'unavailable',
  rejected_by_validators: 'rejected',
  unknown_state: 'unavailable',
  fail_unavailable: 'unavailable',
  fail_rejected: 'rejected',
};

const kind: fc.Arbitrary<Kind> = fc.oneof(
  { weight: 12, arbitrary: fc.constant<Kind>('pending') },
  {
    weight: 1,
    arbitrary: fc.constantFrom<Kind>(
      'ready',
      'unavailable',
      'rejected_by_validators',
      'unknown_state',
      'fail_unavailable',
      'fail_rejected',
      'fail_aborted',
    ),
  },
);

/** Latencia corta, larga, más allá del tope o ninguna respuesta. */
const latency = fc.oneof(
  { weight: 6, arbitrary: fc.integer({ min: 0, max: 3_000 }) },
  { weight: 2, arbitrary: fc.integer({ min: 3_000, max: 30_000 }) },
  { weight: 1, arbitrary: fc.integer({ min: 30_000, max: 150_000 }) },
  { weight: 1, arbitrary: fc.constant(null) },
);

export const assistantScenario: fc.Arbitrary<AssistantScenario> = fc.record({
  accept: fc.record({ kind, latency: fc.integer({ min: 0, max: 5_000 }) }),
  responses: fc.array(fc.record({ kind, latency }), { maxLength: 40 }),
  ticks: fc.array(fc.integer({ min: 1, max: 9_000 }), { minLength: 1, maxLength: 60 }),
});

const REQUEST_ID = '0190a1b2-5555-7000-8000-0000000000aa';

function view(state: string): AssistantRequestView {
  return {
    request_id: REQUEST_ID,
    assistant_state: state,
    unavailable_cause: null,
    draft:
      state === 'ready'
        ? {
            draft_text: 'Borrador sintético',
            citations: [],
            insufficient_evidence_declared: false,
            proposed_cause_category: null,
            model: 'modelo-sintetico',
            model_version: '1',
          }
        : null,
  };
}

class ScriptedFailure extends Error {
  constructor(readonly failure: AssistantFailure) {
    super(failure.kind);
  }
}

function respond(item: Kind): AssistantRequestView {
  switch (item) {
    case 'fail_unavailable':
      throw new ScriptedFailure({ kind: 'unavailable', cause: null });
    case 'fail_rejected':
      throw new ScriptedFailure({
        kind: 'rejected',
        detailCode: 'loop_assistant_output_rejected',
        body: null,
      });
    case 'fail_aborted':
      throw new ScriptedFailure({ kind: 'aborted' });
    case 'unknown_state':
      return view('estado_futuro');
    default:
      return view(item);
  }
}

/** Una petición del servidor falso: responde tras `delay` y, como `fetch`, se cancela con `signal`. */
function settleAfter<T>(
  clock: FakeClock,
  delay: number | null,
  outcome: () => T,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer =
      delay === null
        ? null
        : clock.setTimeout(() => {
            try {
              resolve(outcome());
            } catch (error) {
              reject(error instanceof Error ? error : new Error(String(error)));
            }
          }, delay);
    signal?.addEventListener('abort', () => {
      if (timer !== null) clock.clearTimeout(timer);
      reject(new ScriptedFailure({ kind: 'aborted' }));
    });
  });
}

async function drain(): Promise<void> {
  for (let round = 0; round < 20; round += 1) await Promise.resolve();
}

export async function checkAssistantScenario({
  accept,
  responses,
  ticks,
}: AssistantScenario): Promise<ScenarioOutcome> {
  const clock = new FakeClock(1_700_000_000_000);
  const calls: number[] = [];
  /** Instante en que el servidor falso respondió cada consulta (`null`: no respondió). */
  const answered: (number | null)[] = [];
  // En un objeto: el suscriptor lo asigna y el análisis de flujo no lo ve desde aquí.
  const observed: { startedAt: number | null } = { startedAt: null };
  const poller = createAssistantPoller({
    clock,
    request: () => settleAfter(clock, accept.latency, () => respond(accept.kind)),
    status: (_id, signal) => {
      const index = calls.length;
      calls.push(clock.now());
      answered.push(null);
      const item = responses[index] ?? { kind: 'pending' as const, latency: 0 };
      return settleAfter(
        clock,
        item.latency,
        () => {
          answered[index] = clock.now();
          return respond(item.kind);
        },
        signal,
      );
    },
    classify: (error) =>
      error instanceof ScriptedFailure ? error.failure : { kind: 'unavailable', cause: null },
  });
  poller.subscribe(() => {
    const state = poller.getState();
    if (state.status === 'polling' && observed.startedAt === null)
      observed.startedAt = state.startedAt;
  });
  poller.request();
  // `advance` ejecuta en orden los temporizadores de su ventana y las promesas corren al cierre
  // de ella: el controlador procesa cada respuesta al final de la ventana en la que llegó, igual
  // que un navegador la procesa al llegar.
  const ends: number[] = [];
  let elapsed = 0;
  for (let i = 0; elapsed < HORIZON_MS; i += 1) {
    const step = ticks[i % ticks.length] ?? 1_000;
    clock.advance(step);
    elapsed += step;
    ends.push(clock.now());
    await drain();
  }
  const handledAt = (at: number) => ends.find((end) => end >= at) ?? Number.POSITIVE_INFINITY;
  const final = poller.getState();
  poller.dispose();

  // Nunca queda una solicitud ni una consulta en curso, ni un temporizador vivo.
  expect(['requesting', 'polling']).not.toContain(final.status);
  expect(clock.pendingCount).toBe(0);
  if (observed.startedAt === null) {
    // La solicitud no quedó `pending`: ni una consulta.
    expect(calls).toEqual([]);
    expect(final.status).toBe(EXPECTED_STATUS[accept.kind] ?? 'unavailable');
    return 'not_accepted';
  }
  const accepted = observed.startedAt;
  calls.forEach((at, index) => {
    // Nunca pasado el tope de 2 min ni antes de 5 s desde la consulta anterior.
    expect(at - accepted).toBeLessThan(120_000);
    expect(at - (index === 0 ? accepted : (calls[index - 1] ?? accepted))).toBeGreaterThanOrEqual(
      5_000,
    );
  });
  // La primera respuesta final procesada antes del tope detiene la consulta.
  const first = calls.findIndex((_, index) => {
    const item = responses[index];
    const at = answered[index];
    return (
      item !== undefined &&
      TERMINAL.has(item.kind) &&
      at != null &&
      handledAt(at) - accepted < 120_000
    );
  });
  if (first < 0) {
    expect(final).toEqual({ status: 'unavailable', cause: null, expired: true });
    return 'expired';
  }
  expect(calls).toHaveLength(first + 1);
  const item = responses[first];
  expect(final.status).toBe(item === undefined ? undefined : EXPECTED_STATUS[item.kind]);
  return 'terminal';
}
