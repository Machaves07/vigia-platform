/**
 * Gancho de UI-22: un controlador por montaje con las operaciones que la pantalla le da al
 * montar (las de un análisis concreto); al desmontar cancela la consulta en curso y sus
 * temporizadores. Con `resumeRequestId` (un análisis retomado, VIG-363), al montar vuelve a
 * consultar esa solicitud para mostrar su borrador marcado.
 */
import { useEffect, useState, useSyncExternalStore } from 'react';

import { createAssistantPoller, type AssistantPollerOptions } from './poller';
import type { AssistantPanelState } from './state';

export function useAssistantDraft(
  options: AssistantPollerOptions & { readonly resumeRequestId?: string | null },
): {
  readonly state: AssistantPanelState;
  readonly request: () => void;
} {
  const [poller] = useState(() => createAssistantPoller(options));
  const resume = options.resumeRequestId ?? null;
  useEffect(() => {
    if (resume !== null) poller.resume(resume);
    return () => {
      poller.dispose();
    };
  }, [poller, resume]);
  const state = useSyncExternalStore(poller.subscribe, poller.getState);
  return { state, request: poller.request };
}
