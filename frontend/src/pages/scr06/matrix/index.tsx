/**
 * Grupo `matrix` de SCR-06 (IF-13 paso 3): matriz de pases y registro de pases con evidencia. El
 * `Clock` del armazón se inyecta cuando exista (VIG-320); hasta entonces se usa el reloj del
 * sistema, el mismo que usan `ports.base` y `state.query`.
 */
import { useParams } from 'react-router';

import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { systemClock } from '@/state/clock';

import { MatrixScreen } from './MatrixScreen';

export default function Page() {
  const zoneId = useParams().zone_id;
  if (zoneId === undefined || zoneId === '') return <NotAvailableInScope />;
  return <MatrixScreen zoneId={zoneId} clock={systemClock} />;
}
