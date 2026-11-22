/**
 * `SignalReadings` (TASK-587, O-2 paso a): las señales de un tramo con rol y valor etiquetados, la
 * marca de no válida y la hora de lectura (UI-25) tal como la da el servidor, en la zona de la
 * planta; agregada si la zona es unipersonal (BR-LAZ-06). Sin `read_at`, su ausencia. Las
 * comprobaciones van por `data-part` y clave de texto, por igualdad. Datos sintéticos.
 */
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { SignalReadings } from '@/pages/shared/SignalReadings';
import type { SignalReading } from '@/ports/shared';
import { text } from '@/texts';
import { label } from '@/texts/labels';

afterEach(cleanup);

const SIGNALS: SignalReading[] = [
  {
    signal_id: '0190a1b2-7777-7000-8000-000000000001',
    role: 'energy',
    value: 'asserted',
    valid: true,
    read_at: '2026-01-01T12:00:07.000Z',
  },
  {
    signal_id: null,
    role: 'guard',
    value: 'deasserted',
    valid: false,
    read_at: '2026-01-01T12:00:09.250Z',
  },
  { signal_id: null, role: 'start_command', value: 'asserted', valid: true, read_at: null },
];

function part(root: Element, name: string): Element | null {
  return root.querySelector(`[data-part="${name}"]`);
}

function items(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('[data-part="signal"]')];
}

describe('SignalReadings (TASK-587)', () => {
  it('rol y valor etiquetados, la marca de no válida y la hora de lectura o su ausencia', async () => {
    const { container } = render(
      <SignalReadings
        signals={SIGNALS}
        name="Tramo sintético"
        timeZone="America/Bogota"
        aggregated={false}
      />,
    );
    const rows = items(container);
    expect(rows.map((row) => part(row, 'signal_role')?.textContent)).toEqual([
      label('signal_role', 'energy'),
      label('signal_role', 'guard'),
      label('signal_role', 'start_command'),
    ]);
    expect(rows.map((row) => part(row, 'signal_value')?.textContent)).toEqual([
      label('signal_value', 'asserted'),
      label('signal_value', 'deasserted'),
      label('signal_value', 'asserted'),
    ]);
    // Solo la segunda es no válida, con su marca.
    expect(rows.map((row) => row.dataset.valid)).toEqual(['true', 'false', 'true']);
    expect(rows.map((row) => part(row, 'signal_invalid')?.textContent ?? null)).toEqual([
      null,
      text('signal_readings.invalid'),
      null,
    ]);
    // La hora de `read_at` en la zona de la planta (UTC−5), sin redondear: el oráculo es literal.
    expect(rows.map((row) => row.querySelector('time')?.getAttribute('dateTime') ?? null)).toEqual([
      '2026-01-01T12:00:07.000Z',
      '2026-01-01T12:00:09.250Z',
      null,
    ]);
    expect(rows.map((row) => row.querySelector('time')?.textContent ?? null)).toEqual([
      '01/01/2026, 07:00:07',
      '01/01/2026, 07:00:09',
      null,
    ]);
    expect(part(container, 'zone')?.textContent).toBe('(America/Bogota, UTC-05:00)');
    // Sin `read_at`, «hora de lectura no disponible», nunca otra hora.
    expect(rows.map((row) => part(row, 'signal_read_at_unavailable')?.textContent ?? null)).toEqual(
      [null, null, text('signal_readings.read_at_unavailable')],
    );
    expect(part(container, 'signal_aggregated')).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('en una zona unipersonal la hora es la agregada que da el servidor, marcada como tal', () => {
    const { container } = render(
      <SignalReadings
        signals={[{ ...SIGNALS[0], read_at: '2026-01-01T12:00:00.000Z' } as SignalReading]}
        name="Tramo sintético"
        timeZone="America/Bogota"
        aggregated
      />,
    );
    const [row] = items(container);
    if (row === undefined) throw new Error('Sin señal.');
    expect(row.querySelector('time')?.getAttribute('dateTime')).toBe('2026-01-01T12:00:00.000Z');
    expect(row.querySelector('time')?.textContent).toBe('01/01/2026, 07:00:00');
    expect(part(row, 'signal_aggregated')?.textContent).toBe(text('signal_readings.aggregated'));
  });

  it('un tramo sin señales lo dice, sin lista vacía', () => {
    const { container } = render(
      <SignalReadings
        signals={[]}
        name="Tramo sintético"
        timeZone="America/Bogota"
        aggregated={false}
      />,
    );
    expect(container.querySelector('ul')).toBeNull();
    expect(part(container, 'no_signals')?.textContent).toBe(text('signal_readings.none'));
  });
});
