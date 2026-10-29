/**
 * Paginación de listas largas (sistema visual v1, «Tabla compacta»; VIG-366): «1–20 de 37» con
 * Anterior y Siguiente, para que la flota, el vocabulario, las notificaciones o la cola no midan
 * miles de píxeles. Pagina en el cliente lo ya recibido; si el servidor tiene más (`more`),
 * «Siguiente» en la última página pide la página siguiente (`onLoadMore`) y el total se dice como
 * «37 o más». Nunca calcula un total que el servidor no dio.
 */
import { useState } from 'react';

import { text } from '@/texts';

import styles from './Pager.module.css';

/** Elementos por página por omisión. */
export const PAGE_SIZE = 20;

export interface Paged<T> {
  readonly visible: readonly T[];
  readonly page: number;
  readonly setPage: (page: number) => void;
}

/** La página visible de `items`; si la lista se acorta, vuelve a la última página que existe. */
export function usePaged<T>(items: readonly T[], size: number = PAGE_SIZE): Paged<T> {
  const [page, setPage] = useState(0);
  const last = Math.max(0, Math.ceil(items.length / size) - 1);
  const current = Math.min(page, last);
  return {
    visible: items.slice(current * size, current * size + size),
    page: current,
    setPage,
  };
}

export function Pager({
  page,
  total,
  size = PAGE_SIZE,
  onPage,
  more = false,
  loading = false,
  onLoadMore,
  label,
}: {
  readonly page: number;
  /** Elementos ya recibidos. */
  readonly total: number;
  readonly size?: number;
  readonly onPage: (page: number) => void;
  /** El servidor tiene más elementos que no se han pedido. */
  readonly more?: boolean;
  readonly loading?: boolean;
  readonly onLoadMore?: () => void;
  /** Nombre accesible de la navegación (p. ej. «Páginas de la flota»). */
  readonly label: string;
}) {
  const pages = Math.max(1, Math.ceil(total / size));
  const lastLoaded = page >= pages - 1;
  if (total <= size && !more) return null;
  const start = total === 0 ? 0 : page * size + 1;
  const end = Math.min(total, (page + 1) * size);
  const canNext = !lastLoaded || (more && onLoadMore !== undefined);
  return (
    <nav className={styles.pager} aria-label={label} data-part="pager">
      <span className={styles.range} data-part="range" aria-live="polite">
        {`${String(start)}–${String(end)} ${text('components_structure.pager.of')} ${String(total)}${
          more ? ` ${text('components_structure.pager.or_more')}` : ''
        }`}
      </span>
      <button
        type="button"
        disabled={page === 0}
        onClick={() => {
          onPage(page - 1);
        }}
      >
        {text('components_structure.pager.previous')}
      </button>
      <button
        type="button"
        disabled={!canNext || loading}
        aria-busy={loading}
        onClick={() => {
          if (lastLoaded) onLoadMore?.();
          onPage(page + 1);
        }}
      >
        {text('components_structure.pager.next')}
      </button>
    </nav>
  );
}
