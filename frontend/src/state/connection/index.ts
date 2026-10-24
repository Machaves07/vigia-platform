import type { Clock } from '../clock';

import {
  initialConnection,
  reduceConnection,
  type CircuitEvent,
  type ConnectionSnapshot,
  type Dependency,
} from './reducer';

export type { ConnectionSnapshot, ConnectionState, Dependency } from './reducer';
export { startConnectionProbe } from './probe';

export type Outcome<Error> = { readonly ok: true } | { readonly ok: false; readonly error: Error };

/** No recibe operaciones de escritura: no puede conservarlas, reintentarlas ni encolarlas. */
export function createConnection<Error>(options: {
  readonly clock: Clock;
  readonly counts_for_circuit: (error: Error, dependency: Dependency) => boolean;
}) {
  let snapshot = initialConnection(options.clock.now());
  const listeners = new Set<() => void>();
  const dispatch = (event: CircuitEvent) => {
    const next = reduceConnection(snapshot, event, options.clock.now());
    if (JSON.stringify(next) === JSON.stringify(snapshot)) return;
    snapshot = next;
    for (const listener of listeners) listener();
  };
  const tick = () => {
    dispatch({ type: 'tick' });
  };
  const getSnapshot = (): ConnectionSnapshot => {
    tick();
    return snapshot;
  };
  const canWrite = () => getSnapshot().circuits.platform.state !== 'open';
  return {
    getSnapshot,
    tick,
    canRead: (dep: Dependency) => getSnapshot().circuits[dep].state !== 'open',
    canWrite,
    writeBlockReason: () => (canWrite() ? null : 'sin conexión con la plataforma'),
    register: (dep: Dependency, outcome: Outcome<Error>) => {
      dispatch({
        type: 'register',
        dependency: dep,
        outcome: outcome.ok
          ? 'success'
          : options.counts_for_circuit(outcome.error, dep)
            ? 'failure'
            : 'ignored',
      });
    },
    /** La sonda ya distingue respuesta correcta de fallo de transporte/HTTP. */
    registerProbe: (dep: Dependency, success: boolean) => {
      dispatch({ type: 'register', dependency: dep, outcome: success ? 'success' : 'failure' });
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
