/**
 * `state.scope` (LC-APP-07): `ScopeSelection` y `UiPreferences`, la única escritura al
 * almacenamiento del navegador. El armazón (VIG-320) crea las dos con `localStorage`, carga
 * `GET /hierarchy` en `setAvailablePlants` y coloca UI-03 `ScopeSelector`; cada lista envía
 * `scopeParams(pantalla)` y el cierre de sesión llama a `clear()` y `reset()`.
 */
import { useSyncExternalStore } from 'react';

import type { ScopeSnapshot, ScopeStore } from './selection';

export {
  createPreferencesStore,
  PREFERENCES_KEY,
  type PersistableFilterField,
} from './preferences';
export {
  createScopeStore,
  plantsFromHierarchy,
  type AvailablePlant,
  type ScopeSnapshot,
  type ScopeStore,
} from './selection';

/** La selección vigente, renderizando de nuevo al cambiar. */
export function useScope(store: ScopeStore): ScopeSnapshot {
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}
