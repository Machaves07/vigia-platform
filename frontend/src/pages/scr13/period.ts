/**
 * Periodo por defecto de SCR-13 (VIG-363, VIG-369). Las cifras son mensuales (IF-15 §3) y el mes
 * de negocio es el de la zona horaria de la planta, nunca el de UTC ni el del navegador.
 */
import { formatInTimeZone } from 'date-fns-tz';
import type { Plant } from '@/ports/scr13Metrics';

/** El mes de la planta en el instante `nowMs`, como `yyyy-MM`. */
export function plantMonth(nowMs: number, timeZone: string): string {
  return formatInTimeZone(nowMs, timeZone, 'yyyy-MM');
}

/**
 * El mes en que empieza la semana anterior: el de hoy menos 7 días de calendario de la planta.
 * Se resta sobre la fecha local, no 7 × 24 h, para que un cambio de hora no la mueva de día.
 */
export function previousWeekMonth(nowMs: number, timeZone: string): string {
  const [year, month, day] = formatInTimeZone(nowMs, timeZone, 'yyyy-MM-dd').split('-').map(Number);
  if (year === undefined || month === undefined || day === undefined)
    throw new Error('Fecha inválida.');
  const start = new Date(Date.UTC(year, month - 1, day - 7));
  return `${String(start.getUTCFullYear())}-${String(start.getUTCMonth() + 1).padStart(2, '0')}`;
}

/**
 * La carga inicial: la primera zona del alcance y, en el resumen semanal, el mes de la semana
 * anterior; si no, el mes en curso. Ambos con la zona horaria de la planta de esa zona.
 */
export function defaultPeriod(
  plants: readonly Plant[],
  nowMs: number,
  weekly: boolean,
): { readonly zoneId: string; readonly month: string } | null {
  const plant = plants.find((entry) => entry.zones.length > 0);
  const zone = plant?.zones[0];
  if (plant === undefined || zone === undefined) return null;
  return {
    zoneId: zone.zone_id,
    month: weekly ? previousWeekMonth(nowMs, plant.timezone) : plantMonth(nowMs, plant.timezone),
  };
}
