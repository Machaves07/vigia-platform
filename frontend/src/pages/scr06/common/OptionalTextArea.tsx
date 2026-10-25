/**
 * Texto libre opcional de la consola (IF-13 pasos 5 y 6): el motivo del resultado declarado de una
 * oclusión y el de la aceptación de falsas alarmas. Vacío no viaja; con texto, la política de
 * texto libre la aplica el servidor y su aviso llega anclado aquí, conservando lo escrito.
 */
import { useId } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { text } from '@/texts';

import styles from './common.module.css';

export interface OptionalTextAreaProps {
  readonly label: string;
  readonly hint: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  /** Aviso del servidor anclado a este campo. */
  readonly notice?: Notice | undefined;
  /** Error estructural del espejo del esquema (`validateBody`), si lo hubo. */
  readonly errorKey?: string | null;
}

export function OptionalTextArea({
  label,
  hint,
  value,
  onChange,
  notice,
  errorKey = null,
}: OptionalTextAreaProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const invalid = notice !== undefined || errorKey !== null;
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}
      </label>
      <p id={hintId} className={styles.hint}>
        {hint}
      </p>
      <textarea
        id={id}
        className={styles.input}
        value={value}
        aria-invalid={invalid}
        aria-describedby={invalid ? `${hintId} ${errorId}` : hintId}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      {notice !== undefined ? <FieldNotice id={errorId} notice={notice} /> : null}
      {notice === undefined && errorKey !== null ? (
        <p id={errorId} className={styles.error} data-part="local_error" data-text-key={errorKey}>
          {text(errorKey)}
        </p>
      ) : null}
    </div>
  );
}
