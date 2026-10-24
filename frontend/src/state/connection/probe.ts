import type { Clock, TimerHandle } from '../clock';

import { DEPENDENCIES, type ConnectionSnapshot, type Dependency } from './reducer';

interface ProbeConnection {
  getSnapshot(): ConnectionSnapshot;
  registerProbe(dep: Dependency, success: boolean): void;
  subscribe(listener: () => void): () => void;
}

/** Sonda pública por origen explícito: la salud de platform no prueba la de otro almacén. */
export function startConnectionProbe(options: {
  readonly connection: ProbeConnection;
  readonly clock: Clock;
  readonly document: Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'>;
  readonly fetch: typeof fetch;
  readonly origin: (dep: Dependency) => string;
}): () => void {
  const { clock, connection, document } = options;
  const active = new Map<Dependency, { abort: AbortController; timeout: TimerHandle }>();
  let timer: TimerHandle | undefined;
  let stopped = false;
  let syncing = false;

  const cancel = (dep: Dependency) => {
    const request = active.get(dep);
    if (!request) return;
    active.delete(dep);
    clock.clearTimeout(request.timeout);
    request.abort.abort();
  };

  const launch = (dep: Dependency) => {
    const abort = new AbortController();
    const finish = (success: boolean) => {
      if (active.get(dep)?.abort !== abort) return; // Ignora respuestas tardías o canceladas.
      cancel(dep);
      connection.registerProbe(dep, success);
      sync();
    };
    const timeout = clock.setTimeout(() => {
      finish(false);
    }, 10_000);
    active.set(dep, { abort, timeout });
    // También captura un adaptador fetch que lance antes de devolver la promesa.
    void Promise.resolve()
      .then(() => {
        if (active.get(dep)?.abort !== abort) return;
        return options
          .fetch(new URL('/health/live', options.origin(dep)), {
            method: 'GET',
            signal: abort.signal,
            cache: 'no-store',
            credentials: 'omit',
            redirect: 'error',
          })
          .then(
            (response) => {
              finish(response.ok);
            },
            () => {
              finish(false);
            },
          );
      })
      .catch(() => {
        finish(false);
      });
  };

  function sync() {
    if (stopped || syncing) return;
    syncing = true;
    try {
      if (timer !== undefined) clock.clearTimeout(timer);
      timer = undefined;
      if (document.visibilityState !== 'visible') {
        for (const dep of active.keys()) cancel(dep);
        return;
      }
      const { circuits } = connection.getSnapshot();
      let next = Infinity;
      for (const dep of DEPENDENCIES) {
        const circuit = circuits[dep];
        // Otro puerto puede reabrir mientras la sonda anterior sigue en vuelo.
        if (circuit.state === 'open') cancel(dep);
        if (circuit.state === 'closed') cancel(dep);
        else if (!active.has(dep)) {
          if (circuit.state === 'half_open') launch(dep);
          else if (circuit.retry_at !== null) next = Math.min(next, circuit.retry_at);
        }
      }
      if (Number.isFinite(next)) timer = clock.setTimeout(sync, Math.max(0, next - clock.now()));
    } finally {
      syncing = false;
    }
  }

  const unsubscribe = connection.subscribe(sync);
  document.addEventListener('visibilitychange', sync);
  sync();
  return () => {
    stopped = true;
    unsubscribe();
    document.removeEventListener('visibilitychange', sync);
    if (timer !== undefined) clock.clearTimeout(timer);
    for (const dep of active.keys()) cancel(dep);
  };
}
