/**
 * Campos de fecha, fecha y hora, y mes en español (VIG-366, «Español sin avisos nativos»).
 *
 * Reemplazan `<input type="date">`, `datetime-local` y `month`, que pintan el formato del idioma
 * del navegador (`mm/dd/yyyy`, «October 2026», «--------- ----»): la fecha se escribe
 * `dd/mm/aaaa`, la hora `hh:mm` de 24 horas y el mes se elige de una lista con su nombre en
 * español. El valor que reciben y entregan es el del campo nativo (`aaaa-mm-dd`,
 * `aaaa-mm-ddThh:mm`, `aaaa-mm`), y vacío mientras lo escrito no es una fecha válida. Con `name`, un
 * campo oculto lleva ese valor en `FormData`.
 *
 * Un texto que no es una fecha muestra su propio error en español (`aria-invalid`,
 * `aria-describedby`) al salir del campo o al comprobar el formulario (`invalid`), y deja el campo
 * inválido para `useFormCheck`, que no repite el mensaje (`data-own-error`). Nunca aparece la
 * burbuja del navegador: los formularios llevan `noValidate`.
 */
import { useEffect, useId, useRef, useState } from 'react';

import { text } from '@/texts';

import { FieldError } from './FieldError';
import styles from './DateFields.module.css';
import { dateText, monthText, parseDateText, parseTimeText, recentMonths } from './parse';

interface CommonProps {
  readonly id?: string;
  readonly name?: string;
  readonly required?: boolean;
  readonly disabled?: boolean;
  readonly 'aria-invalid'?: boolean;
  readonly 'aria-describedby'?: string | undefined;
  readonly 'aria-label'?: string;
}

function describedBy(...ids: readonly (string | undefined | false)[]): string | undefined {
  const joined = ids.filter((value) => typeof value === 'string' && value !== '').join(' ');
  return joined === '' ? undefined : joined;
}

/** Texto escrito por la persona que se sincroniza con el valor controlado sin pisar lo que escribe. */
function useTypedText(
  value: string | undefined,
  initial: string,
  toText: (value: string) => string,
  parse: (typed: string) => string | null,
) {
  const [typed, setTyped] = useState(() => toText(value ?? initial));
  const [seen, setSeen] = useState(value);
  if (value !== undefined && value !== seen) {
    setSeen(value);
    if (value !== (parse(typed) ?? '')) setTyped(toText(value));
  }
  return [typed, setTyped] as const;
}

/** Campo de texto con error de formato propio: la validez la marca `setCustomValidity`. */
function TypedInput({
  id,
  typed,
  invalid,
  errorKey,
  placeholder,
  required,
  disabled,
  fieldName,
  ariaInvalid,
  ariaDescribedBy,
  ariaLabel,
  onType,
}: {
  readonly id: string;
  readonly typed: string;
  readonly invalid: boolean;
  readonly errorKey: string;
  readonly placeholder: string;
  readonly required: boolean | undefined;
  readonly disabled: boolean | undefined;
  readonly fieldName: string | undefined;
  readonly ariaInvalid: boolean | undefined;
  readonly ariaDescribedBy: string | undefined;
  readonly ariaLabel: string | undefined;
  readonly onType: (typed: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [touched, setTouched] = useState(false);
  const message = text(errorKey);
  useEffect(() => {
    input.current?.setCustomValidity(invalid ? message : '');
  }, [invalid, message]);
  const shown = invalid && touched;
  const errorId = `${id}-format`;
  return (
    <>
      <input
        ref={input}
        id={id}
        type="text"
        inputMode="numeric"
        autoComplete="off"
        className={styles.input}
        placeholder={placeholder}
        value={typed}
        required={required}
        disabled={disabled}
        data-field-name={fieldName}
        data-own-error="format"
        aria-label={ariaLabel}
        aria-invalid={shown || ariaInvalid === true}
        aria-describedby={describedBy(ariaDescribedBy, shown && errorId)}
        onChange={(event) => {
          onType(event.currentTarget.value);
        }}
        onBlur={() => {
          setTouched(true);
        }}
        onInvalid={() => {
          setTouched(true);
        }}
      />
      {shown && <FieldError id={errorId} textKey={errorKey} />}
    </>
  );
}

export interface DateInputProps extends CommonProps {
  /** `aaaa-mm-dd` o vacío (controlado). */
  readonly value?: string;
  /** `aaaa-mm-dd` inicial sin controlar. */
  readonly defaultValue?: string;
  readonly onChange?: (value: string) => void;
}

export function DateInput({
  id,
  name,
  value,
  defaultValue = '',
  onChange,
  required,
  disabled,
  'aria-invalid': ariaInvalid,
  'aria-describedby': ariaDescribedBy,
  'aria-label': ariaLabel,
}: DateInputProps) {
  const fallbackId = useId();
  const [typed, setTyped] = useTypedText(value, defaultValue, dateText, parseDateText);
  const parsed = parseDateText(typed);
  const invalid = typed.trim() !== '' && parsed === null;
  return (
    <span className={styles.group} data-part="date_input">
      <TypedInput
        id={id ?? fallbackId}
        typed={typed}
        invalid={invalid}
        errorKey="components_dates.date_format"
        placeholder={text('components_dates.date_placeholder')}
        required={required}
        disabled={disabled}
        fieldName={name}
        ariaInvalid={ariaInvalid}
        ariaDescribedBy={ariaDescribedBy}
        ariaLabel={ariaLabel}
        onType={(next) => {
          setTyped(next);
          onChange?.(parseDateText(next) ?? '');
        }}
      />
      {name !== undefined && <input type="hidden" name={name} value={parsed ?? ''} />}
    </span>
  );
}

export interface DateTimeInputProps extends CommonProps {
  /** `aaaa-mm-ddThh:mm` o vacío (controlado). */
  readonly value?: string;
  readonly defaultValue?: string;
  readonly onChange?: (value: string) => void;
}

function splitDateTime(value: string): { date: string; time: string } {
  const [date = '', time = ''] = value.split('T');
  return { date, time: time.slice(0, 5) };
}

/** Fecha `dd/mm/aaaa` y hora `hh:mm` en dos campos; el valor solo existe con las dos válidas. */
export function DateTimeInput({
  id,
  name,
  value,
  defaultValue = '',
  onChange,
  required,
  disabled,
  'aria-invalid': ariaInvalid,
  'aria-describedby': ariaDescribedBy,
  'aria-label': ariaLabel,
}: DateTimeInputProps) {
  const fallbackId = useId();
  const baseId = id ?? fallbackId;
  const initial = splitDateTime(value ?? defaultValue);
  const [date, setDate] = useState(initial.date);
  const [typedTime, setTypedTime] = useState(initial.time);
  const time = parseTimeText(typedTime);
  const combined = date !== '' && time !== null ? `${date}T${time}` : '';
  // Un valor controlado que cambia desde fuera (p. ej. el formulario se vacía) manda.
  const [seen, setSeen] = useState(value);
  if (value !== undefined && value !== seen) {
    setSeen(value);
    if (value !== combined) {
      const next = splitDateTime(value);
      setDate(next.date);
      setTypedTime(next.time);
    }
  }
  const emit = (nextDate: string, nextTime: string | null) => {
    onChange?.(nextDate !== '' && nextTime !== null ? `${nextDate}T${nextTime}` : '');
  };
  return (
    <span className={styles.group} data-part="date_time_input">
      <DateInput
        id={baseId}
        value={date}
        {...(required === undefined ? {} : { required })}
        {...(disabled === undefined ? {} : { disabled })}
        {...(ariaInvalid === undefined ? {} : { 'aria-invalid': ariaInvalid })}
        {...(ariaLabel === undefined ? {} : { 'aria-label': ariaLabel })}
        aria-describedby={ariaDescribedBy}
        onChange={(next) => {
          setDate(next);
          emit(next, time);
        }}
      />
      <TypedInput
        id={`${baseId}-time`}
        typed={typedTime}
        invalid={typedTime.trim() !== '' && time === null}
        errorKey="components_dates.time_format"
        placeholder={text('components_dates.time_placeholder')}
        required={required}
        disabled={disabled}
        fieldName={name}
        ariaInvalid={ariaInvalid}
        ariaDescribedBy={ariaDescribedBy}
        ariaLabel={text('components_dates.time_label')}
        onType={(next) => {
          setTypedTime(next);
          emit(date, parseTimeText(next));
        }}
      />
      {name !== undefined && <input type="hidden" name={name} value={combined} />}
    </span>
  );
}

export interface MonthInputProps extends CommonProps {
  /** `aaaa-mm` o vacío (controlado). */
  readonly value?: string;
  readonly defaultValue?: string;
  readonly onChange?: (value: string) => void;
  /** Mes más reciente que se ofrece (`aaaa-mm`), normalmente el actual en la planta. */
  readonly latest: string;
  /** Cuántos meses se ofrecen hacia atrás desde `latest`. */
  readonly count?: number;
}

/** Lista de meses con su nombre en español («Octubre de 2026»), del más reciente al más antiguo. */
export function MonthInput({
  id,
  name,
  value,
  defaultValue,
  onChange,
  latest,
  count = 24,
  required,
  disabled,
  'aria-invalid': ariaInvalid,
  'aria-describedby': ariaDescribedBy,
  'aria-label': ariaLabel,
}: MonthInputProps) {
  const current = value ?? defaultValue ?? '';
  const months = recentMonths(latest, count);
  const offered = current === '' || months.includes(current) ? months : [current, ...months];
  return (
    <select
      id={id}
      name={name}
      className={styles.select}
      required={required}
      disabled={disabled}
      aria-invalid={ariaInvalid}
      aria-describedby={ariaDescribedBy}
      aria-label={ariaLabel}
      {...(value === undefined ? { defaultValue: current } : { value })}
      onChange={(event) => {
        onChange?.(event.currentTarget.value);
      }}
    >
      <option value="">{text('components_dates.month_choose')}</option>
      {offered.map((month) => (
        <option key={month} value={month}>
          {monthText(month)}
        </option>
      ))}
    </select>
  );
}
