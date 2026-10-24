/**
 * Textos de UI-11 que dependen de los datos (BR-APP-41): etiqueta de cada intervalo, base de reloj
 * y duración exacta. Todo «no observable» y toda causa de la plataforma dicen «no se observó»,
 * nunca «no ocurrió»; las causas del nodo llevan su etiqueta neutral de U-01.
 */
import { causeLabel } from '@/components/ObservabilityBadge/encoding';
import type { CoverageIntervalOut } from '@/state/coverage/compose';
import { text } from '@/texts';

const UNITS = [
  ['day', 24 * 60 * 60 * 1000],
  ['hour', 60 * 60 * 1000],
  ['minute', 60 * 1000],
  ['second', 1000],
  ['millisecond', 1],
] as const;

/** Duración exacta, de días a milisegundos, sin unidades a cero (`2 d 3 h 15 s`). */
export function formatDuration(ms: number): string {
  if (!Number.isSafeInteger(ms) || ms < 0) return text('components_status.timestamp.unavailable');
  if (ms === 0) return `0 ${text('coverage.duration.millisecond')}`;
  const parts: string[] = [];
  let rest = ms;
  for (const [unit, size] of UNITS) {
    const count = Math.floor(rest / size);
    rest -= count * size;
    if (count > 0) parts.push(`${String(count)} ${text(`coverage.duration.${unit}`)}`);
  }
  return parts.join(' ');
}

const KNOWN_STATES = new Set(['observable', 'degraded', 'not_observable']);
const PLATFORM_CAUSES = new Set(['no_communication', 'never_reported', 'zone_not_active']);

/** Lo que dice un intervalo: estado y causas en la capa del nodo, causa en la de plataforma. */
export function intervalLabel(interval: CoverageIntervalOut): string {
  if (interval.layer === 'platform_communication') {
    const cause = interval.causes[0];
    return cause !== undefined && PLATFORM_CAUSES.has(cause)
      ? text(`coverage.cause.${cause}`)
      : text('coverage.state.not_observable');
  }
  const state = KNOWN_STATES.has(interval.state)
    ? text(`coverage.state.${interval.state}`)
    : text('coverage.state.unknown');
  if (interval.causes.length === 0) return state;
  const causes = interval.causes.map(
    (cause) => causeLabel(cause) ?? text('components_status.observability.cause_unknown'),
  );
  return `${state} (${causes.join(', ')})`;
}

/** Base de reloj de un intervalo o de una capa (`clock_basis`, BR-APP-41). */
export function clockBasisLabel(basis: string): string {
  return basis === 'node' || basis === 'platform'
    ? text(`coverage.clock_basis.${basis}`)
    : text('coverage.clock_basis.unknown');
}
