/**
 * Confirmación al abandonar una pantalla con cambios (BR-APP-20).
 *
 * Con `dirty`, registra `beforeunload` (recargar o cerrar la pestaña: el navegador muestra su
 * propio aviso) y bloquea la navegación del enrutador hacia otra ruta hasta que la persona
 * decide; sin cambios no registra nada ni bloquea. Exige el enrutador en modo de datos. El texto
 * del diálogo lo pone el componente (UI-15); el gancho solo expone la decisión pendiente.
 */
import { useEffect } from 'react';
import { useBlocker } from 'react-router';

export interface LeaveGuard {
  /** Hay una navegación detenida esperando la decisión. */
  readonly confirming: boolean;
  /** Abandonar la pantalla y perder los cambios. */
  readonly leave: () => void;
  /** Quedarse en la pantalla. */
  readonly stay: () => void;
}

function onBeforeUnload(event: BeforeUnloadEvent): void {
  // Basta en los navegadores de `browserslist`; `returnValue` está obsoleto.
  event.preventDefault();
}

export function useLeaveGuard(dirty: boolean): LeaveGuard {
  useEffect(() => {
    if (!dirty) return undefined;
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
    };
  }, [dirty]);

  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && currentLocation.pathname !== nextLocation.pathname,
  );

  return {
    confirming: blocker.state === 'blocked',
    leave: () => {
      if (blocker.state === 'blocked') blocker.proceed();
    },
    stay: () => {
      if (blocker.state === 'blocked') blocker.reset();
    },
  };
}
