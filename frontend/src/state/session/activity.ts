/**
 * Puerta de actividad (D-4, nota de BLM §7 y PR-APP-04): tras 25 minutos sin interacción de la
 * persona (clic, tecla o desplazamiento) se cierra, y la siguiente interacción la reabre.
 *
 * `state.query` la consulta en cada tic del refresco periódico (`RefreshGates.isActive`): con la
 * puerta cerrada no sale ningún sondeo de 60 s, así que ninguna lectura en segundo plano renueva
 * la sesión en el servidor y la inactividad puede vencer (U-02 no cambia). La puerta no programa
 * temporizadores: se evalúa con el `Clock` cuando se consulta.
 */
import type { Clock } from '../clock';

/** Sin interacción durante este tiempo, la puerta se cierra (D-4) `[objetivo propio]`. */
export const ACTIVITY_GATE_MS = 25 * 60_000;

/** Interacciones de la persona que reabren la puerta. */
export const ACTIVITY_EVENTS = ['click', 'keydown', 'scroll'] as const;

export interface ActivityGate {
  /** `true` si hubo interacción en los últimos 25 min. */
  isOpen(): boolean;
  /** Instante (`Clock`) de la última interacción; al crear la puerta, el de su creación. */
  lastActivityAt(): number;
  /** Anota una interacción. */
  record(): void;
  /** Deja de escuchar. */
  dispose(): void;
}

export function createActivityGate(options: {
  readonly clock: Clock;
  /** Dónde escuchar; por omisión `window`. `null`: solo `record()`. */
  readonly target?: EventTarget | null;
}): ActivityGate {
  const { clock } = options;
  const target = options.target === undefined ? globalThis.window : options.target;
  let last = clock.now();
  const record = () => {
    last = Math.max(last, clock.now());
  };
  // En captura: el desplazamiento no burbujea, y una pantalla que detiene la propagación de un
  // clic o una tecla sigue contando como interacción.
  const listen = { capture: true, passive: true } as const;
  for (const type of ACTIVITY_EVENTS) target?.addEventListener(type, record, listen);
  return {
    isOpen: () => clock.now() - last < ACTIVITY_GATE_MS,
    lastActivityAt: () => last,
    record,
    dispose: () => {
      for (const type of ACTIVITY_EVENTS) target?.removeEventListener(type, record, listen);
    },
  };
}
