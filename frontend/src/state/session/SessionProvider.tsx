/**
 * Da el almacén de la sesión a la aplicación (LC-APP-06): `useUiSession()` para leer `UiSession`
 * (armazón, enrutador, avisos de UI-05) y `useFormDraft()` para los formularios que deben
 * sobrevivir al bloqueo por reautenticación (BR-APP-16, NFR-APP-38).
 */
import {
  createContext,
  useContext,
  useEffect,
  useReducer,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from 'react';

import {
  createFormDraft,
  formDraftReducer,
  type FormDraft,
  type FormDraftEvent,
} from '../reducers/formDraft';

import type { UiSession } from './reducer';
import type { SessionStore } from './store';

const SessionContext = createContext<SessionStore | null>(null);

export function SessionProvider(props: {
  readonly store: SessionStore;
  readonly children?: ReactNode;
}): ReactNode {
  const { store } = props;
  // Al volver a la pestaña, los vencimientos se reevalúan sin esperar al temporizador.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') store.tick();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [store]);
  return <SessionContext.Provider value={props.store}>{props.children}</SessionContext.Provider>;
}

export function useSession(): SessionStore {
  const store = useContext(SessionContext);
  if (store === null) throw new Error('Falta SessionProvider por encima del componente.');
  return store;
}

export function useUiSession(): UiSession {
  const store = useSession();
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
}

/**
 * Borrador en memoria de un formulario: arranca con el que dejó el bloqueo (misma persona y misma
 * ruta) o vacío, y al desmontarse con la sesión perdida lo deja en el almacén.
 */
export function useFormDraft(
  screenId: string,
  formId: string,
): readonly [FormDraft, (event: FormDraftEvent) => void] {
  const store = useSession();
  const [draft, dispatch] = useReducer(
    formDraftReducer,
    null,
    () => store.takeDraft(screenId, formId) ?? createFormDraft(screenId, formId),
  );
  const latest = useRef(draft);
  useEffect(() => {
    latest.current = draft;
  }, [draft]);
  useEffect(
    () => () => {
      store.keepDraft(latest.current);
    },
    [store],
  );
  return [draft, dispatch] as const;
}
