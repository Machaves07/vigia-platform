/**
 * Valores de proceso sintéticos de un tramo (TASK-596; A-129): velocidad y metros (analógicas),
 * jumbo, una puerta que cambia a +12 s con más cambios, el puente grúa y las puertas sin dato en
 * toda la ventana. Datos de máquina inventados: ningún registro real.
 *
 * `READ_AT` es la hora que da el servidor para el instante del hecho; difiere a propósito del
 * inicio del tramo de las pruebas, para que una hora calculada en la consola no pase.
 */
import { onTestFinished } from 'vitest';
import type { ProcessContext, ProcessVariable, ProcessVariableSummary } from '@/ports/shared';
import { server } from '../msw/server';

/**
 * Todas las peticiones que llegan a MSW desde ahora (método y ruta, ordenadas): para comprobar
 * que pintar los valores de proceso no añade ninguna lectura (TASK-596, «sin lectura nueva»).
 */
export function requestsSeen(): { readonly sorted: () => string[] } {
  const seen: string[] = [];
  const listener = ({ request }: { readonly request: Request }) => {
    seen.push(`${request.method} ${new URL(request.url).pathname}`);
  };
  server.events.on('request:start', listener);
  onTestFinished(() => {
    server.events.removeListener('request:start', listener);
  });
  return { sorted: () => [...seen].sort() };
}

export const READ_AT = '2026-01-01T11:20:10.000Z';
/** Lo que vale la lectura de metros que no es válida en el instante del hecho: no se muestra. */
const LAST_METERS = 18_185;

function variable(
  id: number,
  changes: Partial<ProcessVariable> & Pick<ProcessVariable, 'kind' | 'at_event'>,
): ProcessVariable {
  return {
    variable_id: `0190a1b2-8888-7000-8000-${String(id).padStart(12, '0')}`,
    code: null,
    description_es: null,
    unit: null,
    decimals: null,
    states: null,
    min: null,
    max: null,
    last: null,
    samples: [],
    changes: [],
    truncated: null,
    ...changes,
  };
}

export function processContext(readAt = READ_AT): ProcessContext {
  return {
    from_offset_ms: -30_000,
    to_offset_ms: 20_000,
    variables: [
      variable(1, {
        kind: 'analog',
        code: 'VEL-MAQ',
        description_es: 'Velocidad de la máquina',
        unit: 'm_per_min',
        decimals: 0,
        at_event: { value: 1350, valid: true, offset_ms: 0, read_at: readAt },
        min: 1340,
        max: 1360,
        last: 1355,
        samples: [
          { offset_ms: -30_000, value: 1340 },
          { offset_ms: -10_000, value: null },
          { offset_ms: 0, value: 1350 },
          { offset_ms: 10_000, value: 1360 },
        ],
      }),
      variable(2, {
        kind: 'analog',
        code: 'MET-JUMBO',
        description_es: 'Metros del jumbo',
        unit: 'm',
        decimals: 1,
        at_event: { value: null, valid: false, offset_ms: 1_500, read_at: readAt },
        last: LAST_METERS,
      }),
      variable(3, {
        kind: 'binary',
        code: 'JUMBO-POS',
        description_es: 'Jumbo en posición',
        states: 'in_position_out',
        at_event: { value: 'asserted', valid: true, offset_ms: 0, read_at: readAt },
        changes: [{ offset_ms: -30_000, value: 'asserted' }],
        truncated: false,
      }),
      variable(4, {
        kind: 'binary',
        code: 'P1',
        description_es: 'Puerta 1',
        states: 'closed_open',
        at_event: { value: 'asserted', valid: true, offset_ms: 0, read_at: readAt },
        changes: [
          { offset_ms: -30_000, value: 'asserted' },
          { offset_ms: 12_000, value: 'deasserted' },
        ],
        truncated: true,
      }),
      variable(5, {
        kind: 'binary',
        code: 'GRUA',
        description_es: 'Puente grúa',
        states: 'at_rest_moving',
        at_event: { value: 'unknown', valid: false, offset_ms: 0, read_at: readAt },
        changes: [{ offset_ms: -30_000, value: 'unknown' }],
      }),
      variable(6, {
        kind: 'binary',
        code: 'P1-P4',
        description_es: 'Puertas P1-P4',
        states: 'closed_open',
        at_event: { value: null, valid: false, offset_ms: 0, read_at: null },
      }),
    ],
  };
}

/** El resumen del hito de detección (sin serie): velocidad, jumbo y el puente grúa sin dato. */
export function processSummary(): ProcessVariableSummary[] {
  return processContext()
    .variables.filter((item) => ['VEL-MAQ', 'JUMBO-POS', 'GRUA'].includes(item.code ?? ''))
    .map((item) => ({
      variable_id: item.variable_id,
      kind: item.kind,
      code: item.code,
      description_es: item.description_es,
      unit: item.unit,
      decimals: item.decimals,
      states: item.states,
      value: item.at_event.value,
      valid: item.at_event.valid,
      read_at: item.at_event.read_at,
    }));
}

/** La línea de estados esperada, entera y por igualdad. */
export const STATES_LINE =
  'Jumbo en posición: en posición · Puerta 1: cerrada → abierta a +12 s y más cambios · ' +
  'Sin dato: Puente grúa, Puertas P1-P4';

/** El resumen esperado del hito de detección, entero y por igualdad. */
export const SUMMARY_LINE =
  'Valores del proceso: Velocidad de la máquina: 1.350 m/min · ' +
  'Jumbo en posición: en posición · Sin dato: Puente grúa';

/** Las partes de una línea analógica, para comparar por igualdad. */
export function analogParts(root: ParentNode): Record<string, string | null>[] {
  return [...root.querySelectorAll('[data-part="process_analog"]')].map((line) => {
    const pick = (name: string) => line.querySelector(`[data-part="${name}"]`)?.textContent ?? null;
    return {
      name: pick('process_name'),
      value: pick('process_value'),
      unit: pick('process_unit'),
      at: line.querySelector('[data-part="process_at"] time')?.getAttribute('dateTime') ?? null,
      aggregated: pick('process_aggregated'),
      min: pick('process_min'),
      max: pick('process_max'),
      noData: pick('process_no_data'),
    };
  });
}
