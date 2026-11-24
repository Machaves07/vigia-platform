/**
 * Valores de proceso del episodio donde se decide (TASK-596; A-129; O-2, O-14; UI-25; BR-LAZ-06;
 * BR-APP-40). Ágil y sin ruido: se lee en una pasada al clasificar.
 *
 * - **Analógica**, una línea: «Velocidad de la máquina: 1.350 m/min a las <hora> · mín 1.340 ·
 *   máx 1.360», con la unidad etiquetada (`process_unit`), los decimales del catálogo y la hora
 *   `read_at` **tal como la da el servidor** (agregada en una zona unipersonal, y se dice). Sin
 *   valor válido en el instante del hecho, «sin dato»: nunca se toma otro número de la ventana.
 *   Al final, la mini serie de la ventana: decorativa (`aria-hidden`), el texto basta.
 * - **Estados**, una sola línea: «Jumbo en posición: en posición · Puerta 1: cerrada → abierta a
 *   +12 s y más cambios · Sin dato: Puente grúa, Puertas P1-P4». Solo se agrupan las que no tienen
 *   dato en toda la ventana; los pares salen de `process_state_pair` (`<par>.asserted`).
 * - Las variables van en el orden del bloque (el del catálogo). Sin bloque, nada.
 *
 * Compartido: lo usan la clasificación (SCR-09), el detalle y la ficha (SCR-08) y el episodio de la
 * sombra (SCR-06), siempre desde la lectura que la pantalla ya hace. Selección explícita de campos:
 * nada del objeto recibido se serializa. Textos en `process_values.json`, del armazón.
 */
import { Fragment, type ReactNode } from 'react';

import type { ProcessContext, ProcessVariable, ProcessVariableSummary } from '@/ports/shared';
import { text } from '@/texts';
import { hasLabel, labelInline } from '@/texts/labels';

import styles from './ProcessValues.module.css';

/** Cómo se pinta un instante del servidor en la pantalla (UI-25, zona de la planta). */
export type ProcessInstantView = (instant: string) => ReactNode;

type Declared = Pick<
  ProcessVariable,
  'kind' | 'code' | 'description_es' | 'unit' | 'decimals' | 'states'
>;

const BINARY = 'binary';
const UNKNOWN = 'unknown';
const SEPARATOR = ' · ';
/** Tope de decimales que acepta `Intl.NumberFormat` sin error. */
const MAX_DECIMALS = 20;

const formatters = new Map<number | null, Intl.NumberFormat>();

/** El número con el formateador de la consola (`es-CO`) y los decimales del catálogo. */
function formatNumber(value: number, decimals: number | null): string {
  const digits = decimals === null ? null : Math.min(Math.max(0, decimals), MAX_DECIMALS);
  let format = formatters.get(digits);
  if (format === undefined) {
    format = new Intl.NumberFormat(
      'es-CO',
      digits === null
        ? { maximumFractionDigits: 2 }
        : { minimumFractionDigits: digits, maximumFractionDigits: digits },
    );
    formatters.set(digits, format);
  }
  return format.format(value);
}

const OFFSET = new Intl.NumberFormat('es-CO', {
  maximumFractionDigits: 1,
  signDisplay: 'exceptZero',
});

/** «+12 s», «−4 s»: desplazamiento respecto del inicio del tramo, tal como llega. */
function offsetText(offsetMs: number): string {
  return `${OFFSET.format(offsetMs / 1000)} ${text('process_values.seconds')}`;
}

/** ¿Hay algo que mostrar? Sin bloque o sin variables, la pantalla no pinta la sección. */
export function hasProcessValues(
  context: ProcessContext | null | undefined,
): context is ProcessContext {
  return context !== null && context !== undefined && context.variables.length > 0;
}

function nameOf(variable: Declared): string {
  return variable.description_es ?? text('process_values.undeclared');
}

function unitOf(variable: Declared): string | null {
  return variable.unit !== null && hasLabel('process_unit', variable.unit)
    ? labelInline('process_unit', variable.unit)
    : null;
}

/** El número válido de una lectura analógica, o `null` (sin dato). */
function analogValue(value: number | string | null, valid: boolean): number | null {
  return valid && typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** El estado etiquetado («cerrada»), o `null` si no hay dato o no tiene etiqueta. */
function stateOf(states: string | null, value: number | string | null): string | null {
  if (typeof value !== 'string' || value === UNKNOWN) return null;
  const key = states === null ? null : `${states}.${value}`;
  if (key !== null && hasLabel('process_state_pair', key)) {
    return labelInline('process_state_pair', key);
  }
  return hasLabel('signal_value', value) ? labelInline('signal_value', value) : null;
}

function eventState(variable: ProcessVariable): string | null {
  return variable.at_event.valid ? stateOf(variable.states, variable.at_event.value) : null;
}

/** Sin dato en toda la ventana: ni en el instante del hecho ni en ningún cambio. */
function binaryWithoutData(variable: ProcessVariable): boolean {
  return (
    eventState(variable) === null &&
    variable.changes.every((change) => stateOf(variable.states, change.value) === null)
  );
}

function NoData() {
  return (
    <span data-part="process_no_data" data-text-key="process_values.no_data">
      {text('process_values.no_data')}
    </span>
  );
}

function Name({ variable }: { readonly variable: Declared }) {
  return (
    <span className={styles.name} data-part="process_name">
      {nameOf(variable)}
    </span>
  );
}

function joined(items: readonly ReactNode[]): ReactNode {
  return items.map((item, index) => (
    <Fragment key={index}>
      {index > 0 && SEPARATOR}
      {item}
    </Fragment>
  ));
}

// --- Mini serie (decorativa) -------------------------------------------------------------------

const WIDTH = 120;
const HEIGHT = 24;
const PAD = 2;

/** La serie de la ventana en SVG propio, sin biblioteca; los huecos (sin valor) cortan la línea. */
function Sparkline({
  samples,
  from,
  to,
}: {
  readonly samples: ProcessVariable['samples'];
  readonly from: number;
  readonly to: number;
}) {
  const points = samples.flatMap((sample) =>
    sample.value !== null && Number.isFinite(sample.value)
      ? [{ offset: sample.offset_ms, value: sample.value }]
      : [],
  );
  if (points.length < 2) return null;
  const values = points.map((point) => point.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const start = Math.min(from, ...points.map((point) => point.offset));
  const end = Math.max(to, ...points.map((point) => point.offset));
  const spanX = end - start || 1;
  const spanY = high - low;
  const coordinate = (offset: number, value: number) => ({
    x: PAD + ((offset - start) / spanX) * (WIDTH - 2 * PAD),
    y: spanY === 0 ? HEIGHT / 2 : PAD + (1 - (value - low) / spanY) * (HEIGHT - 2 * PAD),
  });
  const runs: { x: number; y: number }[][] = [[]];
  for (const sample of samples) {
    const current = runs[runs.length - 1] ?? [];
    if (sample.value === null || !Number.isFinite(sample.value)) {
      if (current.length > 0) runs.push([]);
      continue;
    }
    current.push(coordinate(sample.offset_ms, sample.value));
  }
  return (
    <svg
      className={styles.sparkline}
      width={WIDTH}
      height={HEIGHT}
      viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`}
      aria-hidden="true"
      focusable="false"
      data-part="process_sparkline"
    >
      {runs
        .filter((run) => run.length > 0)
        .map((run, index) =>
          run.length === 1 ? (
            <circle
              key={index}
              cx={run[0]?.x.toFixed(1)}
              cy={run[0]?.y.toFixed(1)}
              r="1.5"
              fill="currentColor"
            />
          ) : (
            <polyline
              key={index}
              points={run.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinejoin="round"
            />
          ),
        )}
    </svg>
  );
}

// --- Analógicas --------------------------------------------------------------------------------

function AnalogLine({
  variable,
  from,
  to,
  instant,
  aggregated,
}: {
  readonly variable: ProcessVariable;
  readonly from: number;
  readonly to: number;
  readonly instant: ProcessInstantView;
  readonly aggregated: boolean;
}) {
  const { at_event: event, decimals } = variable;
  const value = analogValue(event.value, event.valid);
  const shown = value === null ? null : formatNumber(value, decimals);
  const unit = unitOf(variable);
  const min = variable.min === null ? null : formatNumber(variable.min, decimals);
  const max = variable.max === null ? null : formatNumber(variable.max, decimals);
  const range: ReactNode[] = [];
  if ((min !== null || max !== null) && (min !== shown || max !== shown)) {
    if (min !== null) {
      range.push(
        <>
          {text('process_values.min')} <span data-part="process_min">{min}</span>
        </>,
      );
    }
    if (max !== null) {
      range.push(
        <>
          {text('process_values.max')} <span data-part="process_max">{max}</span>
        </>,
      );
    }
  }
  return (
    <li className={styles.line} data-part="process_analog" data-valid={String(shown !== null)}>
      <Name variable={variable} />:{' '}
      {shown === null ? (
        <NoData />
      ) : (
        <>
          <span data-part="process_value">{shown}</span>
          {unit !== null && (
            <>
              {' '}
              <span data-part="process_unit">{unit}</span>
            </>
          )}
          {event.read_at !== null && (
            <>
              {' '}
              <span data-part="process_at">
                {text('process_values.at')} {instant(event.read_at)}
                {aggregated && (
                  <>
                    {' '}
                    (
                    <span data-part="process_aggregated" data-text-key="process_values.aggregated">
                      {text('process_values.aggregated')}
                    </span>
                    )
                  </>
                )}
              </span>
            </>
          )}
        </>
      )}
      {range.length > 0 && (
        <>
          {SEPARATOR}
          <span className={styles.secondary} data-part="process_range">
            {joined(range)}
          </span>
        </>
      )}
      <Sparkline samples={variable.samples} from={from} to={to} />
    </li>
  );
}

// --- Estados -----------------------------------------------------------------------------------

/** «cerrada → abierta a +12 s y más cambios», o el estado del instante del hecho si no cambió. */
function StateSequence({
  variable,
  from,
}: {
  readonly variable: ProcessVariable;
  readonly from: number;
}) {
  const { changes } = variable;
  const more = variable.truncated === true && (
    <>
      {' '}
      <span data-part="process_more_changes" data-text-key="process_values.more_changes">
        {text('process_values.more_changes')}
      </span>
    </>
  );
  if (changes.length < 2) {
    const state = eventState(variable);
    return (
      <>
        {state === null ? <NoData /> : <span data-part="process_state_value">{state}</span>}
        {more}
      </>
    );
  }
  return (
    <>
      {changes.map((change, index) => {
        const state = stateOf(variable.states, change.value);
        const timed = index > 0 || change.offset_ms > from;
        return (
          <Fragment key={`${String(change.offset_ms)}:${String(index)}`}>
            {index > 0 && ' → '}
            <span data-part="process_change" data-offset-ms={String(change.offset_ms)}>
              {state === null ? <NoData /> : <span data-part="process_state_value">{state}</span>}
              {timed && (
                <>
                  {' '}
                  {text('process_values.change_at')}{' '}
                  <span data-part="process_offset">{offsetText(change.offset_ms)}</span>
                </>
              )}
            </span>
          </Fragment>
        );
      })}
      {more}
    </>
  );
}

function StatesLine({
  variables,
  from,
}: {
  readonly variables: readonly ProcessVariable[];
  readonly from: number;
}) {
  if (variables.length === 0) return null;
  const missing = variables.filter(binaryWithoutData);
  const items: ReactNode[] = variables
    .filter((variable) => !binaryWithoutData(variable))
    .map((variable) => (
      <span data-part="process_state" key={variable.variable_id}>
        <Name variable={variable} />: <StateSequence variable={variable} from={from} />
      </span>
    ));
  if (missing.length > 0) {
    items.push(<NoDataGroup key="no_data" names={missing.map(nameOf)} />);
  }
  return (
    <p className={styles.line} data-part="process_states">
      {joined(items)}
    </p>
  );
}

function NoDataGroup({ names }: { readonly names: readonly string[] }) {
  return (
    <span data-part="process_no_data_group" data-text-key="process_values.no_data_group">
      {text('process_values.no_data_group')}:{' '}
      {names.map((name, index) => (
        <Fragment key={index}>
          {index > 0 && ', '}
          <span className={styles.name} data-part="process_name">
            {name}
          </span>
        </Fragment>
      ))}
    </span>
  );
}

/** Los valores de proceso de un tramo; sin bloque (o sin variables), nada. */
export function ProcessValues({
  context,
  name,
  instant,
  aggregated,
}: {
  readonly context: ProcessContext | null | undefined;
  /** Nombre accesible de la lista de analógicas (p. ej. el tramo). */
  readonly name: string;
  readonly instant: ProcessInstantView;
  /** `time_aggregated`: la hora llega agregada a la ventana de la zona (BR-LAZ-06). */
  readonly aggregated: boolean;
}) {
  if (!hasProcessValues(context)) return null;
  const { from_offset_ms: from, to_offset_ms: to } = context;
  const analog = context.variables.filter((variable) => variable.kind !== BINARY);
  const binary = context.variables.filter((variable) => variable.kind === BINARY);
  return (
    <div className={styles.values} data-part="process_values">
      {analog.length > 0 && (
        <ul className={styles.list} aria-label={name} data-part="process_analog_list">
          {analog.map((variable) => (
            <AnalogLine
              key={variable.variable_id}
              variable={variable}
              from={from}
              to={to}
              instant={instant}
              aggregated={aggregated}
            />
          ))}
        </ul>
      )}
      <StatesLine variables={binary} from={from} />
    </div>
  );
}

/**
 * Una línea en el hito de detección de la ficha de vida: el valor de cada variable en el instante
 * del hecho, sin serie ni horas («Valores del proceso: Velocidad de la máquina: 1.350 m/min ·
 * Jumbo en posición: en posición · Sin dato: Puente grúa»).
 */
export function ProcessSummary({
  variables,
}: {
  readonly variables: readonly ProcessVariableSummary[];
}) {
  if (variables.length === 0) return null;
  const items: ReactNode[] = [];
  const missing: string[] = [];
  for (const variable of variables) {
    let value: ReactNode = null;
    if (variable.kind === BINARY) {
      const state = variable.valid ? stateOf(variable.states, variable.value) : null;
      if (state !== null) value = <span data-part="process_state_value">{state}</span>;
    } else {
      const number = analogValue(variable.value, variable.valid);
      if (number !== null) {
        const unit = unitOf(variable);
        value = (
          <>
            <span data-part="process_value">{formatNumber(number, variable.decimals)}</span>
            {unit !== null && (
              <>
                {' '}
                <span data-part="process_unit">{unit}</span>
              </>
            )}
          </>
        );
      }
    }
    if (value === null) {
      missing.push(nameOf(variable));
      continue;
    }
    items.push(
      <span data-part="process_summary_item" key={variable.variable_id}>
        <Name variable={variable} />: {value}
      </span>,
    );
  }
  if (missing.length > 0) items.push(<NoDataGroup key="no_data" names={missing} />);
  return (
    <p className={styles.line} data-part="process_summary">
      <span data-part="process_summary_title" data-text-key="process_values.short_title">
        {text('process_values.short_title')}
      </span>
      {': '}
      {joined(items)}
    </p>
  );
}
