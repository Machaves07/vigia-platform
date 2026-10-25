/**
 * `state.concession` (LC-APP-08): `ActiveConcession` del proveedor. El arranque conecta
 * `transportOptions(store)` a `createTransport` (cabecera `X-Vigia-Concession` y `not_found` bajo
 * concesión); el armazón (VIG-320) coloca UI-04 `ConcessionBanner` y vuelve al panel del
 * proveedor con `onEnded`. `ConcessionProvider` da el almacén a las pantallas: el panel del
 * proveedor (SCR-02) activa con él la concesión elegida («operar bajo esta concesión»).
 */
import {
  createContext,
  createElement,
  useContext,
  useSyncExternalStore,
  type ReactNode,
} from 'react';

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

const ConcessionContext = createContext<ConcessionStore | null>(null);

/** Da el almacén de la concesión a las pantallas (lo monta `App`). */
export function ConcessionProvider(props: {
  readonly store: ConcessionStore;
  readonly children?: ReactNode;
}) {
  return createElement(ConcessionContext.Provider, { value: props.store }, props.children);
}

/** El almacén de la concesión, o `null` fuera de `ConcessionProvider`. */
export function useConcessionStore(): ConcessionStore | null {
  return useContext(ConcessionContext);
}
