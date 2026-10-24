/**
 * Página de relevo de un grupo de rutas (VIG-290): el nombre de la pantalla y el estado
 * `unavailable`. No llama a la red. La tarea de cada pantalla sustituye su
 * `src/pages/scrNN/<grupo>/index.tsx` por la página definitiva.
 */
import type { ReactNode } from 'react';

import { text } from '@/texts';

import { ScreenState } from './ScreenState';
import styles from './ScreenState.module.css';

/** `screen`: `scrNN`; el título sale de `navigation.scrNN`. */
export function RelayPage({ screen }: { readonly screen: `scr${number}` }): ReactNode {
  return (
    <main className={styles.panel}>
      <h1 className={styles.title}>{text(`navigation.${screen}`)}</h1>
      <ScreenState state="unavailable" />
    </main>
  );
}
