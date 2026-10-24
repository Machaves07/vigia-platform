/**
 * PR-APP-11 (BR-APP-40): para todo instante y toda zona horaria IANA de planta, el texto mostrado
 * es el instante en esa zona (oráculo: `formatInTimeZone` de `date-fns-tz`) con la zona visible,
 * y leer de vuelta el texto y su desplazamiento produce el mismo instante al segundo.
 */
import { render } from '@testing-library/react';
import { formatInTimeZone } from 'date-fns-tz';
import fc from 'fast-check';
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';

import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';

const ZONES = Intl.supportedValuesOf('timeZone');

const instant = fc.date({
  min: new Date('1970-01-01T00:00:00.000Z'),
  max: new Date('2100-12-31T23:59:59.999Z'),
  noInvalidDate: true,
});

const LOCAL = /^(\d{2})\/(\d{2})\/(\d{4}),?\s(\d{2}):(\d{2}):(\d{2})$/;
const ZONE = /^\((.+), UTC([+-])(\d{2}):(\d{2})(?::(\d{2}))?\)$/;

/** Instante (ms) que representan el texto local y el desplazamiento mostrados. */
function readBack(local: string, zone: string): { instantMs: number; timeZone: string } {
  const l = LOCAL.exec(local);
  const z = ZONE.exec(zone);
  if (l === null || z === null) throw new Error(`Texto no legible: «${local}» «${zone}»`);
  const [day, month = 0, year = 0, hour, minute, second] = l.slice(1).map(Number);
  const sign = z[2] === '-' ? -1 : 1;
  const offsetMs =
    sign * (Number(z[3]) * 3_600_000 + Number(z[4]) * 60_000 + Number(z[5] ?? 0) * 1000);
  const wallMs = Date.UTC(year, month - 1, day, hour, minute, second);
  return { instantMs: wallMs - offsetMs, timeZone: z[1] ?? '' };
}

describe('PR-APP-11: hora en la zona de la planta, ida y vuelta al segundo', () => {
  it('para todo instante y zona IANA', () => {
    fc.assert(
      fc.property(instant, fc.constantFrom(...ZONES), (date, timeZone) => {
        const iso = date.toISOString();
        const { container, unmount } = render(
          createElement(TimestampWithOffset, { instant: iso, timeZone }),
        );
        try {
          const time = container.querySelector('[data-part="platform"] time');
          const zone = container.querySelector('[data-part="platform"] [data-part="zone"]');
          const local = time?.textContent ?? '';
          expect(time?.getAttribute('dateTime')).toBe(iso);
          expect(local.replace(',', '')).toBe(
            formatInTimeZone(date, timeZone, 'dd/MM/yyyy HH:mm:ss'),
          );
          const back = readBack(local, zone?.textContent ?? '');
          expect(back.timeZone).toBe(timeZone);
          expect(back.instantMs).toBe(Math.floor(date.getTime() / 1000) * 1000);
        } finally {
          unmount();
        }
      }),
      {
        // Hora repetida del cambio de horario y desplazamiento con segundos (UTC−00:44:30).
        examples: [
          [new Date('2026-11-01T05:30:00.000Z'), 'America/New_York'],
          [new Date('2026-11-01T06:30:00.000Z'), 'America/New_York'],
          [new Date('1971-01-01T12:00:00.999Z'), 'Africa/Monrovia'],
        ],
      },
    );
  });
});
