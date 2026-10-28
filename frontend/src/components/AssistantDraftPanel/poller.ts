/**
 * Controlador de la consulta de UI-22 sobre el `Clock` inyectado (PAT-APP-RES-06): pide la
 * solicitud, consulta su estado 5 s después de cada respuesta `pending` y, al cumplirse 2 min
 * desde que el servidor la aceptó, cancela la consulta en curso y deja el panel en «no
 * disponible». Una respuesta que llega tarde (otra solicitud, tras el tope o tras `dispose`) no
 * cambia nada. No conoce el puerto: la pantalla le da las dos operaciones y la clasificación de
 * sus fallos.
 */
import type { Clock, TimerHandle } from '@/state/clock';

import {
  assistantReducer,
  canRequest,
  idleAssistant,
  POLL_INTERVAL_MS,
  POLL_LIMIT_MS,
  type AssistantEvent,
  type AssistantFailure,
  type AssistantPanelState,
  type AssistantRequestView,
} from './state';

export interface AssistantPollerOptions {
  readonly clock: Clock;
  /** SUP-U05-19: crea la solicitud (202 `pending`). */
  readonly request: () => Promise<AssistantRequestView>;
  /** SUP-U05-20: estado de la solicitud; `signal` la cancela al llegar el tope. */
  readonly status: (requestId: string, signal: AbortSignal) => Promise<AssistantRequestView>;
  readonly classify: (error: unknown) => AssistantFailure;
}

export interface AssistantPoller {
  readonly getState: () => AssistantPanelState;
  readonly subscribe: (listener: () => void) => () => void;
  /** Pide un borrador; sin efecto mientras haya una solicitud en curso. */
  readonly request: () => void;
  /**
   * Vuelve a consultar una solicitud ya creada (al retomar un análisis guardado, VIG-363): la
   * consulta de inmediato y sigue como tras aceptarla, con el mismo tope desde ahora.
   */
  readonly resume: (requestId: string) => void;
  readonly dispose: () => void;
}

export function createAssistantPoller(options: AssistantPollerOptions): AssistantPoller {
  const { clock } = options;
  const listeners = new Set<() => void>();
  let state: AssistantPanelState = idleAssistant;
  let generation = 0;
  let next: TimerHandle | null = null;
  let deadline: TimerHandle | null = null;
  let inFlight: AbortController | null = null;

  const dispatch = (event: AssistantEvent) => {
    const updated = assistantReducer(state, event);
    if (updated === state) return;
    state = updated;
    for (const listener of listeners) listener();
  };

  const stop = () => {
    if (next !== null) clock.clearTimeout(next);
    if (deadline !== null) clock.clearTimeout(deadline);
    next = null;
    deadline = null;
    inFlight?.abort();
    inFlight = null;
  };

  /** Tras cada respuesta: otra consulta en 5 s mientras siga `pending`; si no, se detiene. */
  const afterResponse = (run: number) => {
    if (run !== generation) return;
    if (state.status !== 'polling') {
      stop();
      return;
    }
    if (deadline === null) {
      const remaining = Math.max(0, state.startedAt + POLL_LIMIT_MS - clock.now());
      deadline = clock.setTimeout(() => {
        deadline = null;
        if (run !== generation) return;
        dispatch({ type: 'tick', now: clock.now() });
        stop();
      }, remaining);
    }
    next = clock.setTimeout(() => {
      next = null;
      poll(run);
    }, POLL_INTERVAL_MS);
  };

  const poll = (run: number) => {
    if (run !== generation || state.status !== 'polling') return;
    // Nunca se consulta pasado el tope, aunque el temporizador del tope aún no haya corrido.
    if (clock.now() - state.startedAt >= POLL_LIMIT_MS) {
      dispatch({ type: 'tick', now: clock.now() });
      stop();
      return;
    }
    const controller = new AbortController();
    inFlight = controller;
    options.status(state.requestId, controller.signal).then(
      (response) => {
        if (inFlight === controller) inFlight = null;
        if (run !== generation || controller.signal.aborted) return;
        dispatch({ type: 'status', response, now: clock.now() });
        afterResponse(run);
      },
      (error: unknown) => {
        if (inFlight === controller) inFlight = null;
        if (run !== generation || controller.signal.aborted) return;
        dispatch({ type: 'failed', failure: options.classify(error) });
        afterResponse(run);
      },
    );
  };

  return {
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    request: () => {
      if (!canRequest(state)) return;
      stop();
      generation += 1;
      const run = generation;
      dispatch({ type: 'request' });
      options.request().then(
        (response) => {
          if (run !== generation) return;
          dispatch({ type: 'accepted', response, now: clock.now() });
          afterResponse(run);
        },
        (error: unknown) => {
          if (run !== generation) return;
          dispatch({ type: 'failed', failure: options.classify(error) });
          afterResponse(run);
        },
      );
    },
    resume: (requestId) => {
      stop();
      generation += 1;
      const run = generation;
      dispatch({ type: 'resumed', requestId, now: clock.now() });
      poll(run);
    },
    dispose: () => {
      generation += 1;
      stop();
    },
  };
}
