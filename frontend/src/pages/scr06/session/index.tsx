/**
 * Grupo `session` de SCR-06 (IF-13 pasos 1 y 2): la consola de comisionamiento y walk-test de la
 * zona. El `Clock` del armazón se inyecta cuando exista (VIG-320); hasta entonces se usa el reloj
 * del sistema, el mismo que usan `ports.base` y `state.query`.
 */
import { useParams } from 'react-router';

import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { systemClock } from '@/state/clock';

import { SessionScreen } from './SessionScreen';

export default function Page() {
  const zoneId = useParams().zone_id;
  if (zoneId === undefined || zoneId === '') return <NotAvailableInScope />;
  return <SessionScreen zoneId={zoneId} clock={systemClock} />;
}
