/**
 * Gancho de UI-22: un controlador por montaje con las operaciones que la pantalla le da al
 * montar (las de un análisis concreto); al desmontar cancela la consulta en curso y sus
 * temporizadores.
 */
import { useEffect, useState, useSyncExternalStore } from 'react';

import { createAssistantPoller, type AssistantPollerOptions } from './poller';
import type { AssistantPanelState } from './state';

export function useAssistantDraft(options: AssistantPollerOptions): {
  readonly state: AssistantPanelState;
  readonly request: () => void;
} {
  const [poller] = useState(() => createAssistantPoller(options));
  useEffect(
    () => () => {
      poller.dispose();
    },
    [poller],
  );
  const state = useSyncExternalStore(poller.subscribe, poller.getState);
  return { state, request: poller.request };
}
