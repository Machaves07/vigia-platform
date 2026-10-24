/**
 * Cronómetro de presentación de un paso (IF-13 paso 2, BR-APP-45): **solo** muestra el tiempo
 * transcurrido desde el `started_at` que dio el servidor, medido con el `Clock` inyectado.
 *
 * El origen nunca es el montaje de la pantalla ni la hora de pared del navegador: un desfase
 * entre el reloj del navegador y el del servidor no cambia lo que se ve, y un `started_at`
 * posterior al reloj inyectado se presenta como cero (nunca un tiempo negativo).
 */
import { useEffect, useState } from 'react';

import type { Clock } from '@/state/clock';

/** Frecuencia de refresco de la presentación (el reloj mide, la pantalla solo muestra). */
const STOPWATCH_TICK_MS = 1_000;

/** Milisegundos transcurridos desde `startedAt` según `clock`; cero si la marca no es válida. */
function elapsedMs(startedAt: string, clock: Clock): number {
  const started = Date.parse(startedAt);
  if (!Number.isFinite(started)) return 0;
  return Math.max(0, clock.now() - started);
}

/** `HH:MM:SS` con las horas sin tope (un paso puede durar más de 24 h). */
export function formatElapsed(ms: number): string {
  const total = Math.floor((Number.isFinite(ms) && ms > 0 ? ms : 0) / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':');
}

/** Tiempo transcurrido que se repinta cada segundo con el temporizador del mismo `Clock`. */
export function useElapsedMs(startedAt: string | null, clock: Clock): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (startedAt === null) return undefined;
    const handle = clock.setTimeout(() => {
      setTick((value) => value + 1);
    }, STOPWATCH_TICK_MS);
    return () => {
      clock.clearTimeout(handle);
    };
  }, [clock, startedAt, tick]);
  return startedAt === null ? 0 : elapsedMs(startedAt, clock);
}
