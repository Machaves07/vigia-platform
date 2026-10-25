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
/** Inicio civil, acotado al aniversario UTC que valida U-04 (incluido el recorte bisiesto). */
export function reviewInstant(day: string, clock: Clock, timeZone: string): string {
  const upper = new Date(clock.now());
  const year = upper.getUTCFullYear() + 1;
  const last = new Date(Date.UTC(year, upper.getUTCMonth() + 1, 0)).getUTCDate();
  upper.setUTCFullYear(year, upper.getUTCMonth(), Math.min(upper.getUTCDate(), last));
  const start = fromZonedTime(`${day}T00:00:00`, timeZone);
  return new Date(Math.min(start.getTime(), upper.getTime())).toISOString();
}
export function executionInstant(value: string, timeZone: string): string | null {
  const date = fromZonedTime(value, timeZone);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}
