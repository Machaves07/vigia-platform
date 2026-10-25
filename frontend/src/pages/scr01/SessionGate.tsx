/**
 * Puertas de sesión de SCR-01 sobre cualquier ruta (BR-APP-09, BR-APP-13). El armazón (UI-01,
 * VIG-320) la monta como ruta de diseño por encima de `createAppRoutes`, dentro del enrutador y de
 * `SessionProvider`:
 *
 * - `second_factor_pending`: la única ruta es la del código o, si el servidor dijo que el factor
 *   no está inscrito, la de inscripción; cualquier otra URL (también `/home`) redirige a ella.
 * - `privacy_notice_pending`: UI-08 ocupa la pantalla en cualquier ruta; la ruta queda guardada
 *   en `UiSession.return_route` y se vuelve a ella al aceptar.
 *
 * Es comodidad de la interfaz: el servidor rechaza cualquier ruta con sesión hasta completar el
 * segundo factor (`unauthenticated`) o aceptar el aviso (`privacy_notice_required`).
 */
import type { ReactNode } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router';

import { useUiSession } from '@/state/session';

import { PrivacyNoticeGate } from './PrivacyNoticeGate';

const SECOND_FACTOR_ROUTE = '/login/second-factor';
const ENROLL_ROUTE = '/login/enroll';

export function SessionGate({ children }: { readonly children?: ReactNode }): ReactNode {
  const session = useUiSession();
  const { pathname } = useLocation();
  if (session.status === 'privacy_notice_pending') return <PrivacyNoticeGate />;
  if (session.status === 'second_factor_pending') {
    const only = session.second_factor_enrollment_required ? ENROLL_ROUTE : SECOND_FACTOR_ROUTE;
    if (pathname !== only) return <Navigate to={only} replace />;
  }
  return children ?? <Outlet />;
}
