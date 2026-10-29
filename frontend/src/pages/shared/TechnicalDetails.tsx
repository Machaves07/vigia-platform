/**
 * Datos técnicos plegados (sistema visual v1, «Personas, no sistemas»; VIG-366): identificadores,
 * direcciones locales y expresiones que el soporte necesita pero que no se muestran a la vista.
 * Van en un `<details>` cerrado, en fuente monoespaciada y copiables.
 */
import type { ReactNode } from 'react';

import { text } from '@/texts';

import styles from './TechnicalDetails.module.css';

export interface TechnicalItem {
  /** Nombre del dato, ya resuelto desde el catálogo. */
  readonly name: string;
  readonly value: ReactNode;
}

export function TechnicalDetails({
  items,
  summary,
}: {
  readonly items: readonly TechnicalItem[];
  /** Título del pliegue; por omisión «Datos técnicos». */
  readonly summary?: string;
}) {
  if (items.length === 0) return null;
  return (
    <details className={styles.details} data-part="technical">
      <summary>{summary ?? text('common.technical_details')}</summary>
      <dl className={styles.list}>
        {items.map((item) => (
          <div key={item.name}>
            <dt>{item.name}</dt>
            <dd>
              <code>{item.value}</code>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
