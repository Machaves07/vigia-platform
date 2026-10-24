/**
 * Muestra de exposición del tramo 3a (BR-APP-84, interfaces de U-03 v1.2): la consola no mide por
 * su cuenta, solo registra el tramo «recepción de la respuesta del pase → pintado».
 *
 * - `fetched_at` se lee del `Clock` inyectado en el momento de recibir la respuesta del pase.
 * - `displayed_at` se lee del **mismo** reloj en el siguiente fotograma (`requestAnimationFrame`),
 *   así que `displayed_at ≥ fetched_at` siempre.
 * - El envío es en segundo plano y **sin reintento**: el fallo no bloquea el pase ni se repite; el
 *   tramo queda como «no medido» en el acta (la pila de avisos no lo muestra como error de la
 *   persona).
 */
import type { ExposureSampleBody } from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';

/** Desenlace del envío de una muestra. */
export type ExposureOutcome = 'sent' | 'failed';

export interface ExposureSamplerOptions {
  readonly clock: Clock;
  /** Programa el fotograma siguiente (`requestAnimationFrame` en el navegador). */
  readonly nextFrame: (callback: () => void) => void;
  readonly send: (sample: ExposureSampleBody) => Promise<unknown>;
  readonly onOutcome?: (passId: string, outcome: ExposureOutcome) => void;
}

export interface ExposureSampler {
  /** Programa la muestra del pase: una sola vez, sin esperar al envío. */
  sample(passId: string): void;
  /** Las muestras programadas, para las pruebas y el resumen de la pantalla. */
  readonly samples: readonly ExposureSampleBody[];
}

/** Fotograma siguiente del navegador; sin `requestAnimationFrame` (pruebas), una macrotarea. */
export function nextAnimationFrame(callback: () => void): void {
  if (typeof globalThis.requestAnimationFrame === 'function') {
    globalThis.requestAnimationFrame(() => {
      callback();
    });
    return;
  }
  globalThis.setTimeout(callback, 0);
}

export function createExposureSampler(options: ExposureSamplerOptions): ExposureSampler {
  const samples: ExposureSampleBody[] = [];
  return {
    samples,
    sample(passId: string): void {
      const fetchedAt = options.clock.now();
      options.nextFrame(() => {
        const displayedAt = Math.max(fetchedAt, options.clock.now());
        const body: ExposureSampleBody = {
          pass_id: passId,
          fetched_at: new Date(fetchedAt).toISOString(),
          displayed_at: new Date(displayedAt).toISOString(),
        };
        samples.push(body);
        void options.send(body).then(
          () => {
            options.onOutcome?.(passId, 'sent');
          },
          () => {
            options.onOutcome?.(passId, 'failed');
          },
        );
      });
    },
  };
}
