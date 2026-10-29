/**
 * Marco de las pantallas de acceso (VIG-366, S3-01): la marca «Vigía» con su frase y una tarjeta
 * centrada con el formulario. Antes iban pegadas a la izquierda y sin marca.
 */
import type { ReactNode } from 'react';

import { text } from '@/texts';

import styles from '../scr01.module.css';

export function AuthFrame({
  labelledBy,
  children,
}: {
  readonly labelledBy: string;
  readonly children: ReactNode;
}) {
  return (
    <main className={styles.auth} aria-labelledby={labelledBy}>
      <p className={styles.authBrand} data-part="brand">
        <span className={styles.authName}>{text('common.app_name')}</span>
        <span className={styles.authTagline}>{text('scr01.login.tagline')}</span>
      </p>
      <div className={styles.authCard}>{children}</div>
    </main>
  );
}
