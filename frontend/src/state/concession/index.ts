/**
 * `state.concession` (LC-APP-08): `ActiveConcession` del proveedor. El arranque conecta
 * `transportOptions(store)` a `createTransport` (cabecera `X-Vigia-Concession` y `not_found` bajo
 * concesión); el armazón (VIG-320) coloca UI-04 `ConcessionBanner` y vuelve al panel del
 * proveedor con `onEnded`.
 */
import { useSyncExternalStore } from 'react';

import type { TransportOptions } from '@/ports/base';

import type { ConcessionSnapshot, ConcessionStore } from './store';

export {
  createConcessionStore,
  type ConcessionEnd,
  type ConcessionSnapshot,
  type ConcessionStore,
} from './store';

/** Los proveedores de `ports.base` que conecta la concesión. */
export function transportOptions(
  store: ConcessionStore,
): Required<Pick<TransportOptions, 'concession' | 'onConcessionNotFound'>> {
  return {
    concession: () => store.header(),
    onConcessionNotFound: (concessionId) => {
      store.reportNotFound(concessionId);
    },
  };
}

/** La concesión vigente, renderizando de nuevo al cambiar. */
export function useConcession(store: ConcessionStore): ConcessionSnapshot {
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}
