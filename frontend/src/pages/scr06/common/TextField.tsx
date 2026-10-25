/**
 * Campo de texto de la consola (IF-13): etiqueta, ayuda y el error estructural del campo, que
 * sale del espejo del esquema generado (`validateBody`) o de la comprobación local de presencia.
 * Nunca reemplaza al servidor: solo evita enviar lo que el contrato ya rechaza.
 */
import { useId } from 'react';

import { text } from '@/texts';

import styles from './common.module.css';

export interface TextFieldProps {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly name?: string;
  /** Clave del catálogo del error del campo, o `null`. */
  readonly errorKey?: string | null;
  readonly hint?: string;
  readonly type?: 'text' | 'number';
  readonly min?: number;
  readonly required?: boolean;
}

export function TextField({
  label,
  value,
  onChange,
  name,
  errorKey = null,
  hint,
  type = 'text',
  min,
  required = false,
}: TextFieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy =
    errorKey === null ? (hint === undefined ? undefined : hintId) : `${hintId} ${errorId}`;
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}
        {required ? (
          <>
            {' '}
            <span className={styles.required}>({text('components_forms.reason.required')})</span>
          </>
        ) : null}
      </label>
      {hint === undefined ? null : (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      )}
      <input
        id={id}
        name={name}
        className={styles.input}
        type={type}
        value={value}
        required={required}
        aria-required={required}
        aria-invalid={errorKey !== null}
        aria-describedby={describedBy}
        {...(min === undefined ? {} : { min })}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      {errorKey === null ? null : (
        <p id={errorId} className={styles.error} data-part="local_error" data-text-key={errorKey}>
          {text(errorKey)}
        </p>
      )}
    </div>
  );
}
