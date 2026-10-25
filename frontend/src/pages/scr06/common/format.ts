/**
 * Textos de la consola que dependen de los datos (IF-13): duraciones y plantillas.
 *
 * `formatDurationMs` compone horas, minutos y segundos con las letras de la unidad del catálogo,
 * sin dígitos pegados, y nunca muestra el total de horas por responsable (H-53).
 */
import { text } from '@/texts';

const HOUR_MS = 3_600_000;
const MINUTE_MS = 60_000;
const SECOND_MS = 1_000;

/** Duración legible (`3 h 07 min 05 s`); un valor fuera de rango se presenta como cero. */
export function formatDurationMs(ms: number): string {
  const total = Number.isSafeInteger(ms) && ms > 0 ? ms : 0;
  const hours = Math.floor(total / HOUR_MS);
  const minutes = Math.floor((total % HOUR_MS) / MINUTE_MS);
  const seconds = Math.floor((total % MINUTE_MS) / SECOND_MS);
  return [
    `${String(hours)} ${text('scr06.duration.hour')}`,
    `${String(minutes).padStart(2, '0')} ${text('scr06.duration.minute')}`,
    `${String(seconds).padStart(2, '0')} ${text('scr06.duration.second')}`,
  ].join(' ');
}

/** Un texto del catálogo con su marca sustituida (`{since}`, `{at}`). */
export function fill(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{([a-z_]+)\}/g, (whole, name: string) => values[name] ?? whole);
}
