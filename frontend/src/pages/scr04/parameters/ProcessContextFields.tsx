/**
 * Valores de proceso de la zona (A-129, TASK-594): pocos campos, prellenados y validados en vivo
 * con las reglas del contrato (regla 7 del catálogo). El servidor vuelve a validar y su aviso
 * prevalece; aquí solo se evita enviar lo que ya se sabe que rechaza.
 */
import { useState } from 'react';
import {
  PROCESS_STATE_PAIRS,
  PROCESS_UNITS,
  processDescriptionValid,
  screenLabel,
  type Catalog,
  type ProcessStatePair,
  type ProcessUnit,
} from '@/ports/scr04Parameters';
import { text } from '@/texts';

const MAX_VARIABLES = 12;
const MAX_BEFORE_SECONDS = 120;
const DEFAULT_BEFORE_SECONDS = 30;
const MAX_SCALE = 1e12;
const CODE = /^[A-Z0-9-]{2,32}$/u;
const READER = /^[a-z][a-z0-9_.-]{0,31}$/u;
const INTEGER = /^\d+$/u;

export interface DeclaredSignal {
  readonly signal_id: string;
  readonly code: string;
  readonly description_es: string;
}

export interface VariableDraft {
  readonly variable_id: string;
  readonly code: string;
  readonly kind: 'analog' | 'binary';
  readonly description_es: string;
  readonly reader: string;
  readonly channel: string;
  readonly unit: ProcessUnit;
  readonly at_4_ma: string;
  readonly at_20_ma: string;
  readonly decimals: string;
  readonly signal_id: string;
  readonly states: ProcessStatePair;
}

export interface ProcessDraft {
  readonly before_seconds: string;
  readonly variables: readonly VariableDraft[];
}

type Field = Exclude<keyof VariableDraft, 'variable_id'>;
/** `before_seconds`, `variables` o `<índice>.<campo>` → clave del texto del problema. */
export type ProcessIssues = ReadonlyMap<string, string>;

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
const string = (value: unknown) => (typeof value === 'string' ? value : '');
const numeric = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value) ? String(value) : '';

export function declaredSignals(initial: Catalog): DeclaredSignal[] {
  const signals: unknown = initial.catalog.signals;
  if (!Array.isArray(signals)) return [];
  return signals
    .map((value: unknown) => {
      const signal = record(value);
      return {
        signal_id: string(signal.signal_id),
        code: string(signal.code),
        description_es: string(signal.description_es),
      };
    })
    .filter((signal) => signal.signal_id !== '');
}

/** El borrador desde la versión vigente; sin bloque, la ventana de Pope (30 s) y ninguna. */
export function processDraft(initial: Catalog): ProcessDraft {
  const block = record(initial.catalog.process_context);
  const variables: unknown = block.variables;
  const before = block.before_ms;
  return {
    before_seconds:
      typeof before === 'number' && Number.isInteger(before)
        ? String(before / 1000)
        : String(DEFAULT_BEFORE_SECONDS),
    variables: Array.isArray(variables)
      ? variables.map((value: unknown) => {
          const variable = record(value);
          const source = record(variable.source);
          const scale = record(variable.scale);
          const unit = PROCESS_UNITS.find((item) => item === variable.unit);
          const states = PROCESS_STATE_PAIRS.find((item) => item === variable.states);
          return {
            variable_id: string(variable.variable_id),
            code: string(variable.code),
            kind: variable.kind === 'binary' ? 'binary' : 'analog',
            description_es: string(variable.description_es),
            reader: string(source.reader),
            channel: numeric(source.channel),
            unit: unit ?? 'm_per_min',
            at_4_ma: numeric(scale.at_4_ma),
            at_20_ma: numeric(scale.at_20_ma),
            decimals: numeric(variable.decimals),
            signal_id: string(variable.signal_id),
            states: states ?? 'on_off',
          };
        })
      : [],
  };
}

/** Una variable nueva prellenada: el lector y el canal siguiente de la última analógica. */
function newVariable(
  draft: ProcessDraft,
  signals: readonly DeclaredSignal[],
  variableId: string,
): VariableDraft {
  const analog = draft.variables.filter((item) => item.kind === 'analog');
  const last = analog.at(-1);
  const used = new Set(analog.map((item) => Number(item.channel)));
  let channel = 0;
  while (used.has(channel)) channel += 1;
  return {
    variable_id: variableId,
    code: '',
    kind: 'analog',
    description_es: '',
    reader: last?.reader ?? '',
    channel: String(channel),
    unit: 'm_per_min',
    at_4_ma: '0',
    at_20_ma: '',
    decimals: '0',
    signal_id: signals[0]?.signal_id ?? '',
    states: 'on_off',
  };
}

function number(value: string): number {
  return value.trim() === '' ? NaN : Number(value);
}
function inScale(value: string): boolean {
  const parsed = number(value);
  return Number.isFinite(parsed) && Math.abs(parsed) <= MAX_SCALE;
}
function integerIn(value: string, max: number): boolean {
  return INTEGER.test(value.trim()) && Number(value) <= max;
}

/** Las mismas reglas que el modelo del contrato, por campo y antes de enviar. */
export function processIssues(
  draft: ProcessDraft,
  signals: readonly DeclaredSignal[],
): ProcessIssues {
  const issues = new Map<string, string>();
  if (!integerIn(draft.before_seconds, MAX_BEFORE_SECONDS))
    issues.set('before_seconds', 'scr04.parameters.process_before_range');
  if (draft.variables.length > MAX_VARIABLES)
    issues.set('variables', 'scr04.parameters.process_too_many');
  const declared = new Set(signals.map((signal) => signal.signal_id));
  const codes = new Map<string, number>();
  for (const variable of draft.variables)
    codes.set(variable.code, (codes.get(variable.code) ?? 0) + 1);
  draft.variables.forEach((variable, index) => {
    const at = (field: Field, key: string) => {
      if (!issues.has(`${String(index)}.${field}`)) issues.set(`${String(index)}.${field}`, key);
    };
    if (!CODE.test(variable.code)) at('code', 'scr04.parameters.process_code_format');
    else if ((codes.get(variable.code) ?? 0) > 1)
      at('code', 'scr04.parameters.process_code_repeated');
    if (!processDescriptionValid(variable.description_es))
      at('description_es', 'scr04.parameters.process_description_invalid');
    if (variable.kind === 'binary') {
      if (!declared.has(variable.signal_id))
        at('signal_id', 'scr04.parameters.process_signal_undeclared');
      return;
    }
    if (!READER.test(variable.reader)) at('reader', 'scr04.parameters.process_reader_format');
    if (!integerIn(variable.channel, 255)) at('channel', 'scr04.parameters.process_channel_range');
    if (!inScale(variable.at_4_ma)) at('at_4_ma', 'scr04.parameters.process_scale_number');
    if (!inScale(variable.at_20_ma)) at('at_20_ma', 'scr04.parameters.process_scale_number');
    else if (number(variable.at_4_ma) === number(variable.at_20_ma))
      at('at_20_ma', 'scr04.parameters.process_scale_flat');
    if (!integerIn(variable.decimals, 3)) at('decimals', 'scr04.parameters.process_decimals_range');
  });
  return issues;
}

/** El cuerpo de `PUT /zones/{zone_id}/process-context` (sin el motivo). */
export function processBody(draft: ProcessDraft): Record<string, unknown> {
  return {
    before_ms: number(draft.before_seconds) * 1000,
    variables: draft.variables.map((variable) => {
      const common = {
        variable_id: variable.variable_id,
        code: variable.code,
        kind: variable.kind,
        description_es: variable.description_es.normalize('NFC'),
      };
      return variable.kind === 'binary'
        ? { ...common, signal_id: variable.signal_id, states: variable.states }
        : {
            ...common,
            source: { reader: variable.reader, channel: number(variable.channel) },
            unit: variable.unit,
            scale: { at_4_ma: number(variable.at_4_ma), at_20_ma: number(variable.at_20_ma) },
            decimals: number(variable.decimals),
          };
    }),
  };
}

export function ProcessContextFields({
  draft,
  signals,
  showAll,
  onChange,
  newId = () => crypto.randomUUID(),
}: {
  readonly draft: ProcessDraft;
  readonly signals: readonly DeclaredSignal[];
  /** Tras un intento de envío, todos los problemas; antes, solo los de lo ya tocado. */
  readonly showAll: boolean;
  readonly onChange: (draft: ProcessDraft) => void;
  readonly newId?: () => string;
}) {
  const [touched, setTouched] = useState<ReadonlySet<string>>(() => new Set());
  const issues = processIssues(draft, signals);
  const touch = (key: string) => {
    if (!touched.has(key)) setTouched(new Set([...touched, key]));
  };
  const issueOf = (key: string) => (showAll || touched.has(key) ? issues.get(key) : undefined);
  const setVariable = (index: number, change: Partial<VariableDraft>) => {
    for (const field of Object.keys(change)) touch(`${String(index)}.${field}`);
    onChange({
      ...draft,
      variables: draft.variables.map((item, position) =>
        position === index ? { ...item, ...change } : item,
      ),
    });
  };
  const issueNode = (issue: string | undefined, id: string) =>
    issue === undefined ? null : (
      <span id={id} data-part="process_issue" data-text-key={issue}>
        {text(issue)}
      </span>
    );
  const input = (
    key: string,
    labelKey: string,
    value: string,
    onValue: (value: string) => void,
    type: 'text' | 'number' = 'text',
  ) => {
    const issue = issueOf(key);
    const errorId = `process-${key}-issue`;
    return (
      <>
        <label>
          {text(labelKey)}
          <input
            type={type}
            step={type === 'number' ? 'any' : undefined}
            value={value}
            aria-invalid={issue !== undefined}
            aria-describedby={issue === undefined ? undefined : errorId}
            onChange={(event) => {
              onValue(event.target.value);
            }}
          />
        </label>
        {issueNode(issue, errorId)}
      </>
    );
  };
  const select = <T extends string>(
    key: string,
    labelKey: string,
    value: T,
    options: readonly { readonly value: T; readonly caption: string }[],
    onValue: (value: T) => void,
  ) => {
    const issue = issueOf(key);
    const errorId = `process-${key}-issue`;
    return (
      <>
        <label>
          {text(labelKey)}
          <select
            value={value}
            aria-invalid={issue !== undefined}
            aria-describedby={issue === undefined ? undefined : errorId}
            onChange={(event) => {
              onValue(event.target.value as T);
            }}
          >
            {options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.caption}
              </option>
            ))}
          </select>
        </label>
        {issueNode(issue, errorId)}
      </>
    );
  };
  const declared = signals.map((signal) => ({
    value: signal.signal_id,
    caption: `${signal.code} · ${signal.description_es}`,
  }));
  // Solo las señales de la versión publicada; el aviso vacío, si la elegida ya no está.
  const signalOptions = (current: string) =>
    signals.some((signal) => signal.signal_id === current)
      ? declared
      : [{ value: '', caption: text('scr04.parameters.process_choose_signal') }, ...declared];
  return (
    <>
      <p>{text('scr04.parameters.process_help')}</p>
      {input(
        'before_seconds',
        'scr04.parameters.process_before_seconds',
        draft.before_seconds,
        (value) => {
          touch('before_seconds');
          onChange({ ...draft, before_seconds: value });
        },
        'number',
      )}
      {draft.variables.map((variable, index) => {
        const at = (field: Field) => `${String(index)}.${field}`;
        return (
          <fieldset key={variable.variable_id} data-part="process_variable">
            <legend>{`${text('scr04.parameters.process_variable')} ${String(index + 1)}`}</legend>
            {input(at('code'), 'scr04.parameters.process_code', variable.code, (code) => {
              setVariable(index, { code: code.toUpperCase() });
            })}
            {input(
              at('description_es'),
              'scr04.parameters.process_description',
              variable.description_es,
              (description_es) => {
                setVariable(index, { description_es });
              },
            )}
            {select(
              at('kind'),
              'scr04.parameters.process_kind',
              variable.kind,
              (['analog', 'binary'] as const).map((kind) => ({
                value: kind,
                caption: screenLabel('process_variable_kind', kind),
              })),
              (kind) => {
                setVariable(index, { kind });
              },
            )}
            {variable.kind === 'analog' ? (
              <>
                {input(
                  at('reader'),
                  'scr04.parameters.process_reader',
                  variable.reader,
                  (reader) => {
                    setVariable(index, { reader });
                  },
                )}
                {input(
                  at('channel'),
                  'scr04.parameters.process_channel',
                  variable.channel,
                  (channel) => {
                    setVariable(index, { channel });
                  },
                  'number',
                )}
                {select(
                  at('unit'),
                  'scr04.parameters.process_unit',
                  variable.unit,
                  PROCESS_UNITS.map((unit) => ({
                    value: unit,
                    caption: screenLabel('process_unit', unit),
                  })),
                  (unit) => {
                    setVariable(index, { unit });
                  },
                )}
                {input(
                  at('at_4_ma'),
                  'scr04.parameters.process_at_4_ma',
                  variable.at_4_ma,
                  (at_4_ma) => {
                    setVariable(index, { at_4_ma });
                  },
                  'number',
                )}
                {input(
                  at('at_20_ma'),
                  'scr04.parameters.process_at_20_ma',
                  variable.at_20_ma,
                  (at_20_ma) => {
                    setVariable(index, { at_20_ma });
                  },
                  'number',
                )}
                {input(
                  at('decimals'),
                  'scr04.parameters.process_decimals',
                  variable.decimals,
                  (decimals) => {
                    setVariable(index, { decimals });
                  },
                  'number',
                )}
              </>
            ) : (
              <>
                {select(
                  at('signal_id'),
                  'scr04.parameters.process_signal',
                  variable.signal_id,
                  signalOptions(variable.signal_id),
                  (signal_id) => {
                    setVariable(index, { signal_id });
                  },
                )}
                {select(
                  at('states'),
                  'scr04.parameters.process_states',
                  variable.states,
                  PROCESS_STATE_PAIRS.map((pair) => ({
                    value: pair,
                    caption: screenLabel('process_state_pair', pair),
                  })),
                  (states) => {
                    setVariable(index, { states });
                  },
                )}
              </>
            )}
            <button
              type="button"
              onClick={() => {
                onChange({
                  ...draft,
                  variables: draft.variables.filter((_, position) => position !== index),
                });
                setTouched(new Set());
              }}
            >
              {text('scr04.parameters.process_remove')}
            </button>
          </fieldset>
        );
      })}
      {issueOf('variables') !== undefined && (
        <p data-part="process_issue" data-text-key={issues.get('variables')}>
          {text(issues.get('variables') ?? 'validation.invalid')}
        </p>
      )}
      <button
        type="button"
        disabled={draft.variables.length >= MAX_VARIABLES}
        onClick={() => {
          onChange({
            ...draft,
            variables: [...draft.variables, newVariable(draft, signals, newId())],
          });
        }}
      >
        {text('scr04.parameters.process_add')}
      </button>
    </>
  );
}
