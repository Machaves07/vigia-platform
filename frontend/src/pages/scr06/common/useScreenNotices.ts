/**
 * Avisos de una pantalla de la consola (UI-19): lista en memoria con identificador local, el
 * último aviso de cada campo manda (`noticeForField` de UI-19) y se descartan al cerrarlos.
 *
 * La pila no bloquea nada (BR-APP-43): solo informa de lo que el servidor rechazó.
 */
import { useCallback, useMemo, useRef, useState } from 'react';

import { noticeForField, type Notice } from '@/components/NoticeStack/notice';

import { noticeFromError } from './notices';

export interface ScreenNotices {
  readonly notices: readonly Notice[];
  /** Añade el aviso de `error`; `field` lo ancla a un campo del formulario. */
  add(error: unknown, field?: string | null): void;
  dismiss(id: string): void;
  /** El aviso anclado a `field`, si lo hay. */
  field(field: string): Notice | undefined;
}

export function useScreenNotices(): ScreenNotices {
  const [notices, setNotices] = useState<readonly Notice[]>([]);
  const counter = useRef(0);
  const add = useCallback((error: unknown, field: string | null = null) => {
    counter.current += 1;
    const notice = noticeFromError(error, `scr06-notice-${String(counter.current)}`, field);
    if (notice === null) return;
    setNotices((current) => [...current, notice]);
  }, []);
  return useMemo(
    () => ({
      notices,
      add,
      dismiss(id: string) {
        setNotices((current) => current.filter((notice) => notice.id !== id));
      },
      field: (field: string) => noticeForField(notices, field),
    }),
    [notices, add],
  );
}
