/**
 * Disposición de tableta (sistema visual v1, VIG-364): hasta 960 px de ancho, sin menú lateral y
 * con barra inferior de pestañas. La misma consulta que `@media (max-width: 960px)` de las hojas.
 * Sin `matchMedia` (jsdom) es escritorio.
 */
import { useSyncExternalStore } from 'react';

export const TABLET_QUERY = '(max-width: 960px)';

function media(): MediaQueryList | null {
  return typeof window.matchMedia === 'function' ? window.matchMedia(TABLET_QUERY) : null;
}

function subscribe(listener: () => void): () => void {
  const list = media();
  if (list === null) return () => undefined;
  list.addEventListener('change', listener);
  return () => {
    list.removeEventListener('change', listener);
  };
}

function snapshot(): boolean {
  return media()?.matches ?? false;
}

/** `true` si la ventana tiene el ancho de una tableta. */
export function useTabletLayout(): boolean {
  return useSyncExternalStore(subscribe, snapshot, () => false);
}
