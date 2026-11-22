/**
 * UI-13 `ReasonField` (BR-APP-42, 44; PR-APP-12; A-126): el campo de texto libre con contador de
 * caracteres y los límites de la unidad dueña (concesión 10 a 500; U-03 y U-04 desde el esquema
 * generado con `reasonLimits`). La validación local es el espejo `checkFreeText` de la política
 * base del servidor y nunca es la barrera: si llega un aviso del servidor anclado al campo, su
 * mensaje prevalece sobre el local y lo escrito se conserva.
 *
 * Guía mientras se escribe, en vez de rechazar al final (A-126, regla 3 del principio rector):
 *
 * - `optional`: el campo puede quedar vacío (en blanco se envía `null`);
 * - `loopText`: texto libre del lazo. El contador y el espejo miden **lo que se enviaría**
 *   (`submittedText`: sin los espacios ni saltos de línea de los extremos, en NFC), el formulario
 *   envía ese texto recortado y un 400 sin `detail_code` se explica como revisión del texto
 *   (`TextNotice`). El texto interior nunca se toca sin una acción de la persona;
 * - un salto de línea que se enviaría avisa en vivo, con «Unir en una línea», que lo cambia por un
 *   espacio. Sin pulsarlo, el campo sigue con error y el formulario no envía (el servidor lo
 *   rechazaría como `control_character`).
 */
import { useId, useState } from 'react';

import type { Notice } from '@/components/NoticeStack/notice';
import { checkFreeText, type FreeTextLimits } from '@/ports/validation';
import { codePointLength, type FreeTextRejection } from '@/ports/validation/freeText';
import { text } from '@/texts';

import styles from './ReasonField.module.css';
import { TextNotice } from './TextNotice';

export interface ReasonFieldOptions {
  /** Puede quedar vacío: en blanco no hay error y se envía `null` (`optionalText`). */
  readonly optional?: boolean;
  /** Texto libre del lazo: se mide y se envía recortado en los extremos (`submittedText`). */
  readonly loopText?: boolean;
}

export interface ReasonFieldProps extends ReasonFieldOptions {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly limits: FreeTextLimits;
  readonly name?: string;
  /** Aviso del servidor anclado a este campo (`noticeForField`); prevalece sobre el local. */
  readonly notice?: Notice | undefined;
  /** Muestra el error local aunque el campo no haya perdido el foco (intento de envío). */
  readonly showErrors?: boolean;
}

/** Saltos de línea: el servidor los rechaza como `control_character` (`Cc`, `Zl` y `Zp`). */
const LINE_BREAK = /[\n\v\f\r\u0085\u2028\u2029]/u;
const LINE_BREAKS = /[\n\v\f\r\u0085\u2028\u2029]+/gu;
/** Espacios y saltos de línea de los extremos (propiedad Unicode `White_Space`). */
const EDGES = /^\p{White_Space}+|\p{White_Space}+$/gu;
/** Clave del error local de un salto de línea, y de su aviso en vivo. */
const LINE_BREAK_KEY = 'validation.free_text.line_break';

/** Lo que se envía de un texto del lazo: sin espacios ni saltos de línea en los extremos. */
export function submittedText(value: string): string {
  return value.replace(EDGES, '');
}

/** Un texto opcional del lazo: recortado, o `null` si queda vacío. */
export function optionalText(value: string): string | null {
  const sent = submittedText(value);
  return sent === '' ? null : sent;
}

/** «Unir en una línea»: cada tramo de saltos de línea pasa a ser un espacio; nada más cambia. */
function joinLines(value: string): string {
  return value.replace(LINE_BREAKS, ' ');
}

function measured(value: string, options: ReasonFieldOptions): string {
  return options.loopText === true ? submittedText(value) : value;
}

function blank(sent: string, options: ReasonFieldOptions): boolean {
  // Recortado, un texto del lazo solo está vacío si no queda nada (un invisible suelto no lo es).
  return options.loopText === true ? sent === '' : sent.trim() === '';
}

export type ReasonVerdict =
  | { readonly ok: true; readonly sent: string | null }
  | { readonly ok: false; readonly reason: FreeTextRejection | 'required'; readonly key: string };

/** Veredicto del campo sobre lo que se enviaría: espejo de la política base (PR-APP-12). */
export function reasonVerdict(
  value: string,
  limits: FreeTextLimits,
  options: ReasonFieldOptions = {},
): ReasonVerdict {
  const sent = measured(value, options);
  if (blank(sent, options)) {
    return options.optional === true
      ? { ok: true, sent: null }
      : { ok: false, reason: 'required', key: 'validation.required' };
  }
  const verdict = checkFreeText(sent, limits);
  if (verdict.ok) return { ok: true, sent };
  const key =
    verdict.reason === 'control_character' && LINE_BREAK.test(sent) ? LINE_BREAK_KEY : verdict.key;
  return { ok: false, reason: verdict.reason, key };
}

/** Clave del catálogo del error local, o `null` si el texto pasa el espejo. */
export function reasonIssueKey(
  value: string,
  limits: FreeTextLimits,
  options: ReasonFieldOptions = {},
): string | null {
  const verdict = reasonVerdict(value, limits, options);
  return verdict.ok ? null : verdict.key;
}

export function ReasonField({
  label,
  value,
  onChange,
  limits,
  name,
  notice,
  showErrors = false,
  optional = false,
  loopText = false,
}: ReasonFieldProps) {
  const id = useId();
  const [touched, setTouched] = useState(false);
  const options = { optional, loopText };
  const issue = reasonIssueKey(value, limits, options);
  const sent = measured(value, options);
  // El aviso del salto de línea sale en vivo y hace de error local: no se repite debajo.
  const lineBreak = LINE_BREAK.test(sent);
  const localError =
    notice === undefined && (touched || showErrors) && issue !== null && issue !== LINE_BREAK_KEY;
  const counterId = `${id}-counter`;
  const errorId = `${id}-error`;
  const breakId = `${id}-line-break`;
  const length = codePointLength(sent.normalize('NFC'));
  const described = [
    counterId,
    lineBreak ? breakId : null,
    notice !== undefined || localError ? errorId : null,
  ].filter((part) => part !== null);
  const requirement = optional
    ? 'components_forms.reason.optional'
    : 'components_forms.reason.required';
  const counter = optional
    ? `${String(length)} / ${String(limits.maxLength)} ${text('components_forms.reason.characters')}`
    : `${String(length)} / ${String(limits.maxLength)} ${text('components_forms.reason.characters')} (${text('components_forms.reason.minimum')} ${String(limits.minLength)})`;
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label} <span className={styles.required}>({text(requirement)})</span>
      </label>
      <textarea
        id={id}
        name={name}
        className={styles.input}
        value={value}
        required={!optional}
        aria-required={!optional}
        aria-invalid={notice !== undefined || localError || lineBreak}
        aria-describedby={described.join(' ')}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        onBlur={() => {
          setTouched(true);
        }}
      />
      <span
        id={counterId}
        className={styles.counter}
        data-part="counter"
        data-over={length > limits.maxLength}
      >
        {counter}
      </span>
      {lineBreak ? (
        <div id={breakId} className={styles.lineBreak} data-part="line_break">
          <p role="status" className={styles.error} data-text-key={LINE_BREAK_KEY}>
            {text(LINE_BREAK_KEY)}
          </p>
          <button
            type="button"
            className={styles.join}
            data-part="join_lines"
            onClick={() => {
              onChange(joinLines(value));
            }}
          >
            {text('components_forms.reason.join_lines')}
          </button>
        </div>
      ) : null}
      {notice !== undefined ? (
        <TextNotice id={errorId} notice={notice} written={value} loopText={loopText} />
      ) : null}
      {localError ? (
        <p id={errorId} className={styles.error} data-part="local_error" data-text-key={issue}>
          {text(issue)}
        </p>
      ) : null}
    </div>
  );
}
