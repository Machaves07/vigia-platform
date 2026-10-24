/**
 * Acuse de escritura (BR-APP-08, BR-NUC-16): tras toda escritura confirmada, el registro creado y
 * el `role_in_use` **que devolvió el servidor**. El rol nunca se elige ni se envía: este
 * componente solo lo muestra, con la etiqueta de U-02 y nunca con el valor crudo.
 */
import platform from '../../../../backend/resources/labels.platform.es.json';

import { text } from '@/texts';

import styles from './WriteReceipt.module.css';

export interface WriteReceiptProps {
  /** Nombre legible del registro creado (p. ej. tipo y `code`), compuesto por la pantalla. */
  readonly record: string;
  /** `role_in_use` de la respuesta; `null` si el servidor no atribuyó rol. */
  readonly roleInUse: string | null;
}

const roles: Readonly<Record<string, string>> = platform.role;

function roleLabel(role: string | null): string {
  if (role === null) return text('components_forms.receipt.role_none');
  return Object.hasOwn(roles, role)
    ? (roles[role] ?? text('components_forms.receipt.role_unknown'))
    : text('components_forms.receipt.role_unknown');
}

export function WriteReceipt({ record, roleInUse }: WriteReceiptProps) {
  return (
    <div className={styles.receipt} role="status" data-role-in-use={roleInUse ?? ''}>
      <p className={styles.title}>
        {text('components_forms.receipt.title')}: <span data-part="record">{record}</span>
      </p>
      <p className={styles.role}>
        {text('components_forms.receipt.role_in_use')}:{' '}
        <span data-part="role_in_use">{roleLabel(roleInUse)}</span>
      </p>
    </div>
  );
}
