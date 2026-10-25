/**
 * Campo de motivo obligatorio de la consola (IF-13 pasos 1 y 2; BR-APP-42, 44): la reapertura de
 * una sesión incompleta y la corrección de las horas de un paso exigen motivo, y el servidor lo
 * registra con quién y cuándo.
 *
 * No se usa UI-13 `ReasonField` porque exige los límites (`minLength`/`maxLength`) del esquema
 * espejo generado y el de estas dos operaciones no los fija: `reasonLimits` falla a propósito en
 * ese caso en vez de inventar un límite. Aquí solo se comprueba que haya texto (la política de
 * texto libre del servidor — 10 a 500 caracteres para `correction.reason_es` — manda) y el aviso
 * anclado del servidor, si llega, se muestra igual y conserva lo escrito.
 */
import { useId, useState } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { text } from '@/texts';

import styles from './common.module.css';

export interface ReasonInputProps {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly name?: string;
  /** Aviso del servidor anclado a este campo; prevalece sobre el error local. */
  readonly notice?: Notice | undefined;
  /** Muestra el error local aunque el campo no haya perdido el foco (intento de envío). */
  readonly showErrors?: boolean;
  readonly hint?: string;
  /** Error estructural del espejo del esquema (`validateBody`), si lo hubo. */
  readonly errorKey?: string | null;
}

/** Clave del catálogo del error local, o `null` si el motivo tiene texto. */
function reasonIssueKey(value: string): string | null {
  return value.trim() === '' ? 'validation.required' : null;
}

export function ReasonInput({
  label,
  value,
  onChange,
  name,
  notice,
  showErrors = false,
  hint,
  errorKey = null,
}: ReasonInputProps) {
  const id = useId();
  const [touched, setTouched] = useState(false);
  const issue = errorKey ?? reasonIssueKey(value);
  const localError = notice === undefined && (touched || showErrors) && issue !== null;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}{' '}
        <span className={styles.required}>({text('components_forms.reason.required')})</span>
      </label>
      {hint === undefined ? null : (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      )}
      <textarea
        id={id}
        name={name}
        className={styles.input}
        value={value}
        required
        aria-required="true"
        aria-invalid={notice !== undefined || localError}
        aria-describedby={
          notice !== undefined || localError
            ? `${hintId} ${errorId}`
            : hint === undefined
              ? undefined
              : hintId
        }
        onChange={(event) => {
          onChange(event.target.value);
        }}
        onBlur={() => {
          setTouched(true);
        }}
      />
      {notice !== undefined ? <FieldNotice id={errorId} notice={notice} /> : null}
      {localError ? (
        <p id={errorId} className={styles.error} data-part="local_error" data-text-key={issue}>
          {text(issue)}
        </p>
      ) : null}
    </div>
  );
}
