/**
 * VIG-369: periodo por defecto de SCR-13 para cualquier fecha. El oráculo no reutiliza la
 * aritmética del módulo: recorre la lista de días locales de la planta y toma el de 7 posiciones
 * antes, así que cruza meses, años y cambios de hora por construcción.
 */
import { formatInTimeZone, fromZonedTime } from 'date-fns-tz';
import { describe, expect, it } from 'vitest';
import { defaultPeriod, plantMonth, previousWeekMonth } from '@/pages/scr13/period';
import type { Plant } from '@/ports/scr13Metrics';

// Sin cambio de hora (la de los mundos sintéticos), con cambio en marzo/octubre, con cambio en
// el hemisferio sur, y los dos extremos del día civil.
const ZONES = [
  'America/Bogota',
  'Europe/Madrid',
  'America/Santiago',
  'Pacific/Kiritimati',
  'Pacific/Pago_Pago',
  'UTC',
] as const;
const HOURS = ['00:30:00', '12:00:00', '23:30:00'] as const;
const MONTH = /^[0-9]{4}-(0[1-9]|1[0-2])$/;

/** Días de calendario como cadenas, sin zona horaria: del 24-12-2025 al 08-01-2027. */
function calendar(): string[] {
  const days: string[] = [];
  for (let at = Date.UTC(2025, 11, 24); at <= Date.UTC(2027, 0, 8); at += 86_400_000)
    days.push(new Date(at).toISOString().slice(0, 10));
  return days;
}

function plant(timeZone: string, zones: readonly string[], code = 'P-SINT'): Plant {
  return {
    plant_id: `plant-${code}`,
    code,
    name: 'Planta sintética',
    country: 'CO',
    data_region: 'co',
    timezone: timeZone,
    status: 'active',
    zones: zones.map((zone, index) => ({
      zone_id: zone,
      plant_id: `plant-${code}`,
      code: `Z-${String(index)}`,
      name: `Zona sintética ${String(index + 1)}`,
      node_id: null,
    })),
  };
}

describe('SCR-13 periodo por defecto (VIG-369)', () => {
  it.each([
    // El nightly rojo: domingo 01-11-2026 en la planta; la semana anterior empieza el 25-10.
    ['2026-11-01T00:30:00', 'America/Bogota', '2026-10', '2026-11'],
    ['2026-11-01T23:30:00', 'America/Bogota', '2026-10', '2026-11'],
    ['2026-11-07T23:30:00', 'America/Bogota', '2026-10', '2026-11'],
    ['2026-11-08T00:30:00', 'America/Bogota', '2026-11', '2026-11'],
    // Cruce de año.
    ['2026-12-31T23:30:00', 'America/Bogota', '2026-12', '2026-12'],
    ['2027-01-01T00:30:00', 'America/Bogota', '2026-12', '2027-01'],
    ['2027-01-07T23:30:00', 'America/Bogota', '2026-12', '2027-01'],
    ['2027-01-08T00:30:00', 'America/Bogota', '2027-01', '2027-01'],
    // 21:00 del 07-11 en Bogotá ya es 08-11 en UTC: manda la planta.
    ['2026-11-07T21:00:00', 'America/Bogota', '2026-10', '2026-11'],
    // Santiago adelanta la hora el 06-09-2026: 7 × 24 h desde las 00:30 del 08-09 caerían en agosto.
    ['2026-09-08T00:30:00', 'America/Santiago', '2026-09', '2026-09'],
    // Madrid cambia el 29-03 y el 25-10.
    ['2026-04-05T00:30:00', 'Europe/Madrid', '2026-03', '2026-04'],
    ['2026-11-01T00:30:00', 'Europe/Madrid', '2026-10', '2026-11'],
    // 23:30 del 07-11 en Pago Pago ya es 08-11 en UTC; 00:30 del 08-11 en Kiritimati, aún 07-11.
    ['2026-11-07T23:30:00', 'Pacific/Pago_Pago', '2026-10', '2026-11'],
    ['2026-11-08T00:30:00', 'Pacific/Kiritimati', '2026-11', '2026-11'],
  ])('%s en %s: semana anterior %s, mes en curso %s', (local, timeZone, weekly, monthly) => {
    const now = fromZonedTime(local, timeZone).getTime();
    expect(previousWeekMonth(now, timeZone)).toBe(weekly);
    expect(plantMonth(now, timeZone)).toBe(monthly);
  });

  it('todos los días de un año, a tres horas, en seis zonas: mes consultable y de la semana anterior', () => {
    const days = calendar();
    let checked = 0;
    for (const timeZone of ZONES) {
      // Una planta sin zonas delante: la zona sale del alcance y el mes, de la planta de esa zona.
      const plants = [plant('Pacific/Kiritimati', [], 'A'), plant(timeZone, ['z-1', 'z-2'], 'B')];
      for (let index = 7; index < days.length; index += 1) {
        const today = days[index] ?? '';
        const weekStart = days[index - 7] ?? '';
        for (const hour of HOURS) {
          const now = fromZonedTime(`${today}T${hour}`, timeZone).getTime();
          // Una hora inexistente por el cambio de hora se desplaza; si cambia de día, no cuenta.
          if (formatInTimeZone(now, timeZone, 'yyyy-MM-dd') !== today) continue;
          const weekly = defaultPeriod(plants, now, true);
          const monthly = defaultPeriod(plants, now, false);
          expect(weekly?.zoneId).toBe('z-1');
          expect(monthly?.zoneId).toBe('z-1');
          expect(weekly?.month, `${timeZone} ${today} ${hour}`).toBe(weekStart.slice(0, 7));
          expect(monthly?.month, `${timeZone} ${today} ${hour}`).toBe(today.slice(0, 7));
          for (const month of [weekly?.month ?? '', monthly?.month ?? '']) {
            expect(month).toMatch(MONTH);
            // Lo que el servidor exige del periodo: que ya haya empezado en la hora de la planta.
            expect(fromZonedTime(`${month}-01T00:00:00`, timeZone).getTime()).toBeLessThanOrEqual(
              now,
            );
          }
          checked += 1;
        }
      }
    }
    // 374 días × 3 horas × 6 zonas (6732), menos las horas que no existen ese día.
    expect(checked).toBeGreaterThan(6700);
  });

  it('sin zonas en el alcance no hay carga inicial', () => {
    expect(defaultPeriod([], 0, true)).toBeNull();
    expect(defaultPeriod([plant('UTC', [])], 0, true)).toBeNull();
  });
});
