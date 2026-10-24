/**
 * Presentación de instantes en la zona horaria IANA de la planta (BR-APP-40, PR-APP-11).
 *
 * `Intl.DateTimeFormat` con `es-CO` y el `timeZone` de la planta da el texto y, por sus partes,
 * el desplazamiento de esa zona en ese instante, que se muestra junto al nombre de la zona para
 * que el texto sea inequívoco también en la hora repetida de un cambio de horario. El
 * desplazamiento no sale de `date-fns-tz`: su `getTimezoneOffset` da el de horario estándar en la
 * hora repetida y su `formatInTimeZone` trunca los segundos (Africa/Monrovia hasta 1972,
 * UTC−00:44:30), y ambos rompen la ida y vuelta al segundo; `date-fns-tz` queda como oráculo de
 * PR-APP-11. Nunca se usa la zona del navegador.
 */
import { differenceInMilliseconds } from 'date-fns';

export interface PlantTime {
  /** Instante en ISO 8601 UTC, para el atributo `dateTime`. */
  readonly iso: string;
  /** Fecha y hora locales de la planta, `dd/mm/aaaa, hh:mm:ss` de `es-CO` (24 h). */
  readonly local: string;
  /** Desplazamiento de la zona en ese instante, `UTC±hh:mm` (con `:ss` si lo tiene). */
  readonly offset: string;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatterFor(timeZone: string): Intl.DateTimeFormat {
  let formatter = formatters.get(timeZone);
  if (formatter === undefined) {
    formatter = new Intl.DateTimeFormat('es-CO', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    });
    formatters.set(timeZone, formatter);
  }
  return formatter;
}

function offsetLabel(offsetMs: number): string {
  const sign = offsetMs < 0 ? '-' : '+';
  const totalSeconds = Math.round(Math.abs(offsetMs) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  const tail = seconds === 0 ? '' : `:${pad(seconds)}`;
  return `UTC${sign}${pad(hours)}:${pad(minutes)}${tail}`;
}

function parseInstant(instant: string): Date | null {
  const date = new Date(instant);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Instante en la zona de la planta; `null` si el instante o la zona no son válidos. */
export function plantTime(instant: string, timeZone: string): PlantTime | null {
  const date = parseInstant(instant);
  if (date === null) return null;
  let formatter: Intl.DateTimeFormat;
  try {
    formatter = formatterFor(timeZone);
  } catch {
    return null;
  }
  const field = new Map(formatter.formatToParts(date).map((p) => [p.type, Number(p.value)]));
  const wallMs = Date.UTC(
    field.get('year') ?? Number.NaN,
    (field.get('month') ?? Number.NaN) - 1,
    field.get('day') ?? Number.NaN,
    field.get('hour') ?? Number.NaN,
    field.get('minute') ?? Number.NaN,
    field.get('second') ?? Number.NaN,
  );
  if (Number.isNaN(wallMs)) return null;
  const offsetMs = wallMs - Math.floor(date.getTime() / 1000) * 1000;
  return { iso: date.toISOString(), local: formatter.format(date), offset: offsetLabel(offsetMs) };
}

const offsetFormat = new Intl.NumberFormat('es-CO', {
  signDisplay: 'exceptZero',
  minimumFractionDigits: 0,
  maximumFractionDigits: 1,
});

/** Desfase `node_time − instante` en segundos con signo (`+2,3 s`); `null` si alguno no es válido. */
export function nodeOffset(instant: string, nodeTime: string): string | null {
  const reference = parseInstant(instant);
  const node = parseInstant(nodeTime);
  if (reference === null || node === null) return null;
  return `${offsetFormat.format(differenceInMilliseconds(node, reference) / 1000)} s`;
}
