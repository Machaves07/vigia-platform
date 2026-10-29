/**
 * Error propio de un campo (sistema visual v1, «Campo»): en rojo, con icono y el texto en español
 * del catálogo. El campo lo enlaza con `aria-describedby` y marca `aria-invalid`.
 */
import { text } from '@/texts';

import styles from './DateFields.module.css';

export function FieldError({
  id,
  textKey,
  detail,
}: {
  readonly id: string;
  readonly textKey: string;
  /** Complemento del texto (p. ej. el mínimo de un número). */
  readonly detail?: string;
}) {
  return (
    <span id={id} className={styles.error} data-part="field_error" data-text-key={textKey}>
      <svg
        className={styles.icon}
        viewBox="0 0 16 16"
        aria-hidden="true"
        focusable="false"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      >
        <circle cx="8" cy="8" r="6.5" />
        <path d="M8 4.5v4 M8 11v.5" />
      </svg>
      <span>{detail === undefined ? text(textKey) : `${text(textKey)} ${detail}.`}</span>
    </span>
  );
}
