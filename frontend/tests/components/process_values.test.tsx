/**
 * `ProcessValues` y `ProcessSummary` (TASK-596; A-129; UI-25; BR-LAZ-06): una línea por analógica
 * con valor, unidad, hora del servidor y mínimo y máximo; los estados en una línea con sus cambios
 * y las variables sin dato agrupadas; la mini serie decorativa. Comprobaciones por `data-part`,
 * clave de texto e igualdad, nunca por subcadena. Datos sintéticos.
 */
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { ProcessSummary, ProcessValues } from '@/pages/shared/ProcessValues';
import type { ProcessContext } from '@/ports/shared';
import { text } from '@/texts';
import {
  analogParts,
  processContext,
  processSummary,
  READ_AT,
  STATES_LINE,
  SUMMARY_LINE,
} from '../support/processContext';

afterEach(cleanup);

/** El instante tal como llega, en un `<time>`: lo que la pantalla pinta con UI-25. */
const instant = (value: string) => <time dateTime={value}>{value}</time>;

function mount(context: ProcessContext | null, aggregated = false) {
  return render(
    <ProcessValues
      context={context}
      name="Tramo sintético"
      instant={instant}
      aggregated={aggregated}
    />,
  );
}

function part(root: ParentNode, name: string): Element | null {
  return root.querySelector(`[data-part="${name}"]`);
}

describe('ProcessValues (TASK-596)', () => {
  it('analógicas: valor, unidad, hora del servidor y mínimo y máximo; sin valor válido, «sin dato»', async () => {
    const { container } = mount(processContext());
    expect(analogParts(container)).toEqual([
      {
        name: 'Velocidad de la máquina',
        value: '1.350',
        unit: 'm/min',
        at: READ_AT,
        aggregated: null,
        min: '1.340',
        max: '1.360',
        noData: null,
      },
      {
        name: 'Metros del jumbo',
        value: null,
        unit: null,
        at: null,
        aggregated: null,
        min: null,
        max: null,
        noData: text('process_values.no_data'),
      },
    ]);
    // Nunca otro número de la ventana en lugar del valor del instante del hecho.
    const meters = container.querySelectorAll('[data-part="process_analog"]')[1];
    expect(meters?.getAttribute('data-valid')).toBe('false');
    expect(meters?.textContent).toBe(`Metros del jumbo: ${text('process_values.no_data')}`);
    expect(part(container, 'process_analog_list')?.getAttribute('aria-label')).toBe(
      'Tramo sintético',
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('estados en una línea: el cambio con su desplazamiento, «y más cambios» y los sin dato agrupados', () => {
    const { container } = mount(processContext());
    expect(part(container, 'process_states')?.textContent).toBe(STATES_LINE);
    expect(
      [...container.querySelectorAll('[data-part="process_offset"]')].map(
        (node) => node.textContent,
      ),
    ).toEqual(['+12 s']);
    expect(part(container, 'process_more_changes')?.getAttribute('data-text-key')).toBe(
      'process_values.more_changes',
    );
    const group = part(container, 'process_no_data_group');
    expect(
      [...(group?.querySelectorAll('[data-part="process_name"]') ?? [])].map(
        (node) => node.textContent,
      ),
    ).toEqual(['Puente grúa', 'Puertas P1-P4']);
    expect(container.querySelectorAll('[data-part="process_no_data_group"]')).toHaveLength(1);
  });

  it('zona unipersonal: la hora es la que da el servidor y se marca agregada', () => {
    const aggregatedAt = '2026-01-01T11:15:00.000Z';
    const { container } = mount(processContext(aggregatedAt), true);
    const [speed] = analogParts(container);
    expect(speed?.at).toBe(aggregatedAt);
    expect(speed?.aggregated).toBe(text('process_values.aggregated'));
  });

  it('la mini serie es decorativa (aria-hidden) y se corta en los huecos', () => {
    const { container } = mount(processContext());
    const series = container.querySelectorAll('[data-part="process_sparkline"]');
    // Solo la velocidad tiene serie; los metros no traen muestras.
    expect(series).toHaveLength(1);
    const svg = series[0];
    expect(svg?.getAttribute('aria-hidden')).toBe('true');
    expect(svg?.getAttribute('width')).toBe('120');
    expect(svg?.getAttribute('height')).toBe('24');
    expect(svg?.querySelectorAll('circle')).toHaveLength(1);
    expect(svg?.querySelectorAll('polyline')).toHaveLength(1);
  });

  it('una lectura no válida con valor presente se lee «sin dato», analógica o binaria (m_sin_guarda_valid)', () => {
    const [speed, , jumbo] = processContext().variables;
    if (speed === undefined || jumbo === undefined) throw new Error('Faltan variables.');
    const { container } = mount({
      from_offset_ms: -30_000,
      to_offset_ms: 20_000,
      variables: [
        {
          ...speed,
          at_event: { ...speed.at_event, value: 1350, valid: false },
          min: null,
          max: null,
        },
        { ...jumbo, at_event: { ...jumbo.at_event, value: 'asserted', valid: false }, changes: [] },
      ],
    });
    expect(analogParts(container)).toEqual([
      {
        name: 'Velocidad de la máquina',
        value: null,
        unit: null,
        at: null,
        aggregated: null,
        min: null,
        max: null,
        noData: text('process_values.no_data'),
      },
    ]);
    expect(part(container, 'process_states')?.textContent).toBe('Sin dato: Jumbo en posición');
    cleanup();
    const summary = render(
      <ProcessSummary variables={processSummary().map((item) => ({ ...item, valid: false }))} />,
    );
    expect(part(summary.container, 'process_summary')?.textContent).toBe(
      'Valores del proceso: Sin dato: Velocidad de la máquina, Jumbo en posición, Puente grúa',
    );
  });

  it('sin bloque o sin variables, nada', () => {
    expect(mount(null).container.innerHTML).toBe('');
    cleanup();
    expect(
      mount({ from_offset_ms: -30_000, to_offset_ms: 0, variables: [] }).container.innerHTML,
    ).toBe('');
  });

  it('una variable sin declaración se nombra como tal, sin unidad inventada', () => {
    const [speed] = processContext().variables;
    if (speed === undefined) throw new Error('Sin velocidad.');
    const { container } = mount({
      from_offset_ms: -30_000,
      to_offset_ms: 0,
      variables: [{ ...speed, description_es: null, unit: null, decimals: null }],
    });
    expect(analogParts(container)[0]).toMatchObject({
      name: text('process_values.undeclared'),
      value: '1.350',
      unit: null,
    });
  });
});

describe('ProcessSummary (TASK-596)', () => {
  it('una línea con el valor del instante del hecho y los sin dato agrupados, sin horas', () => {
    const { container } = render(<ProcessSummary variables={processSummary()} />);
    expect(part(container, 'process_summary')?.textContent).toBe(SUMMARY_LINE);
    expect(container.querySelector('time')).toBeNull();
  });

  it('sin variables, nada', () => {
    expect(render(<ProcessSummary variables={[]} />).container.innerHTML).toBe('');
  });
});
