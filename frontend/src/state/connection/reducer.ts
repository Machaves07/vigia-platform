/** Cortacircuitos puros: todos los instantes proceden del reloj del llamador. */
export const DEPENDENCIES = [
  'platform',
  'evidence_store',
  'document_store',
  'live_view_node',
] as const;

export type Dependency = (typeof DEPENDENCIES)[number];
const OPEN_MS = 30_000;
/**
 * Ventana en la que cuentan los fallos. Abarca el ciclo de una lectura que agota sus reintentos
 * por tiempo de espera (U05-H-10: el cuarto fallo es el del tercer reintento): 4 intentos de 10 s
 * separados 2, 4 y 8 s ponen 44 s entre el primer fallo y el cuarto. Con 30 s, una API que no
 * responde nunca abría el circuito (FS-APP-01). Adenda A-70 (decisión del dueño, 2026-10-26):
 * PR-APP-23 se lee con 60 s.
 */
const FAILURE_WINDOW_MS = 60_000;
const FAILURE_THRESHOLD = 4;

interface Circuit {
  readonly state: 'closed' | 'open' | 'half_open';
  readonly failures: readonly number[];
  readonly retry_at: number | null;
}

export interface ConnectionState {
  readonly state: 'online' | 'degraded' | 'offline';
  readonly since: string;
  readonly last_success_at: string | null;
}

export interface ConnectionSnapshot {
  readonly circuits: Readonly<Record<Dependency, Circuit>>;
  readonly connection: ConnectionState;
}

export type CircuitEvent =
  | { readonly type: 'tick' }
  | {
      readonly type: 'register';
      readonly dependency: Dependency;
      readonly outcome: 'success' | 'failure' | 'ignored';
    };

const closed = (): Circuit => ({ state: 'closed', failures: [], retry_at: null });

export function initialConnection(now: number): ConnectionSnapshot {
  return {
    circuits: {
      platform: closed(),
      evidence_store: closed(),
      document_store: closed(),
      live_view_node: closed(),
    },
    connection: { state: 'online', since: new Date(now).toISOString(), last_success_at: null },
  };
}

/** Ventana móvil para contar; apertura completa desde el cuarto fallo (U05-H-10). */
export function reduceConnection(
  previous: ConnectionSnapshot,
  event: CircuitEvent,
  now: number,
): ConnectionSnapshot {
  const circuits = { ...previous.circuits };
  for (const dep of DEPENDENCIES) {
    const circuit = circuits[dep];
    if (circuit.state === 'open' && circuit.retry_at !== null && now >= circuit.retry_at) {
      circuits[dep] = { ...circuit, state: 'half_open' };
    } else if (circuit.state === 'closed') {
      circuits[dep] = {
        ...circuit,
        failures: circuit.failures.filter((at) => now - at < FAILURE_WINDOW_MS),
      };
    }
  }

  let lastSuccess = previous.connection.last_success_at;
  if (event.type === 'register') {
    const circuit = circuits[event.dependency];
    if (event.outcome === 'success') {
      circuits[event.dependency] = closed();
      lastSuccess = new Date(now).toISOString();
    } else if (event.outcome === 'failure' && circuit.state !== 'open') {
      const failures = [...circuit.failures, now];
      circuits[event.dependency] =
        circuit.state === 'half_open' || failures.length >= FAILURE_THRESHOLD
          ? { state: 'open', failures: [], retry_at: now + OPEN_MS }
          : { ...circuit, failures };
    }
  }

  const state =
    circuits.platform.state === 'open'
      ? 'offline'
      : DEPENDENCIES.some(
            (dep) => circuits[dep].state !== 'closed' || circuits[dep].failures.length > 0,
          )
        ? 'degraded'
        : 'online';
  return {
    circuits,
    connection: {
      state,
      since:
        state === previous.connection.state
          ? previous.connection.since
          : new Date(now).toISOString(),
      last_success_at: lastSuccess,
    },
  };
}
