/**
 * Esqueleto de carga y aviso de carga prolongada (NFR-APP-53): el esqueleto aparece en el mismo
 * render, sin números, y el aviso a partir de `LONG_LOAD_MS` medido con el `Clock` inyectado.
 * Montarlo de nuevo reinicia la cuenta: lo monta la pantalla mientras `load_state` es `loading`.
 */
import { useEffect, useState } from 'react';

import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import styles from './RefreshIndicator.module.css';

/** Umbral del aviso de carga prolongada `[objetivo propio]`. */
const LONG_LOAD_MS = 1000;

export function LoadingSkeleton({ clock }: { clock: Clock }) {
  const [long, setLong] = useState(false);
  useEffect(() => {
    const handle = clock.setTimeout(() => {
      setLong(true);
    }, LONG_LOAD_MS);
    return () => {
      clock.clearTimeout(handle);
    };
  }, [clock]);

  return (
    <div className={styles.skeleton} role="status" aria-busy="true">
      <span className={styles.bar} aria-hidden="true" />
      <span className={styles.bar} aria-hidden="true" />
      <span className={styles.bar} aria-hidden="true" />
      <p className={styles.label}>{text('components_status.loading.label')}</p>
      {long && (
        <p className={styles.long} data-part="long-load">
          {text('components_status.loading.long')}
        </p>
      )}
    </div>
  );
}
