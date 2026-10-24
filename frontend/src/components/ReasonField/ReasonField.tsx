/**
 * UI-13 `ReasonField` (BR-APP-42, 44; PR-APP-12): campo de motivo obligatorio con contador de
 * caracteres y los límites de la unidad dueña (concesión 10 a 500; U-03 y U-04 desde el esquema
 * generado con `reasonLimits`). La validación local es el espejo `checkFreeText` de la política
 * base del servidor y nunca es la barrera: si llega un aviso del servidor anclado al campo, su
 * mensaje prevalece sobre el local y lo escrito se conserva.
 */
import { useId, useState } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { checkFreeText, type FreeTextLimits } from '@/ports/validation';
import { codePointLength } from '@/ports/validation/freeText';
import { text } from '@/texts';

import styles from './ReasonField.module.css';

export interface ReasonFieldProps {
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

/** Clave del catálogo del error local, o `null` si el texto pasa el espejo. */
export function reasonIssueKey(value: string, limits: FreeTextLimits): string | null {
  if (value.trim() === '') return 'validation.required';
  const verdict = checkFreeText(value, limits);
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
}: ReasonFieldProps) {
  const id = useId();
  const [touched, setTouched] = useState(false);
  const issue = reasonIssueKey(value, limits);
  const localError = notice === undefined && (touched || showErrors) && issue !== null;
  const counterId = `${id}-counter`;
  const errorId = `${id}-error`;
  const length = codePointLength(value.normalize('NFC'));
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}{' '}
        <span className={styles.required}>({text('components_forms.reason.required')})</span>
      </label>
      <textarea
        id={id}
        name={name}
        className={styles.input}
        value={value}
        required
        aria-required="true"
        aria-invalid={notice !== undefined || localError}
        aria-describedby={
          notice !== undefined || localError ? `${counterId} ${errorId}` : counterId
        }
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
        {`${String(length)} / ${String(limits.maxLength)} ${text('components_forms.reason.characters')} (${text('components_forms.reason.minimum')} ${String(limits.minLength)})`}
      </span>
      {notice !== undefined ? <FieldNotice id={errorId} notice={notice} /> : null}
      {localError ? (
        <p id={errorId} className={styles.error} data-part="local_error" data-text-key={issue}>
          {text(issue)}
        </p>
      ) : null}
    </div>
  );
}
