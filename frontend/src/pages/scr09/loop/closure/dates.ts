/** Fechas civiles de planta: no dependen de la zona del navegador ni de la hora real. */
import { formatInTimeZone, fromZonedTime } from 'date-fns-tz';
import type { Clock } from '@/state/clock';

function plantToday(clock: Clock, timeZone: string): string {
  return formatInTimeZone(clock.now(), timeZone, 'yyyy-MM-dd');
}
export function validReviewDate(day: string, clock: Clock, timeZone: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const today = plantToday(clock, timeZone);
  const year = Number(today.slice(0, 4)) + 1;
  const month = Number(today.slice(5, 7));
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const upper = `${String(year)}-${today.slice(5, 7)}-${String(Math.min(Number(today.slice(8)), last)).padStart(2, '0')}`;
  const parsed = new Date(`${day}T12:00:00Z`);
  return (
    Number.isFinite(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === day &&
    day > today &&
    day <= upper
  );
}
/** Mediodía civil evita que un cambio de hora elimine la medianoche elegida. */
export function reviewInstant(day: string, timeZone: string): string {
  return fromZonedTime(`${day}T12:00:00`, timeZone).toISOString();
}
export function executionInstant(value: string, timeZone: string): string | null {
  const date = fromZonedTime(value, timeZone);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}
