/**
 * Página de relevo de un grupo de rutas (VIG-290): el nombre de la pantalla y el estado
 * `unavailable`. No llama a la red. La tarea de cada pantalla sustituye su
 * `src/pages/scrNN/<grupo>/index.tsx` por la página definitiva.
 */
import type { ReactNode } from 'react';

import { text } from '@/texts';

import { ScreenState } from './ScreenState';
import styles from './ScreenState.module.css';

export function RelayPage({ titleKey }: { readonly titleKey: string }): ReactNode {
  return (
    <main className={styles.panel}>
      <h1 className={styles.title}>{text(titleKey)}</h1>
      <ScreenState state="unavailable" />
    </main>
  );
}
