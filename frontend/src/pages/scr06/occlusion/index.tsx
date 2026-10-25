/**
 * Grupo `occlusion` de SCR-06 (IF-13 paso 5): prueba de oclusión por cámara con verificación
 * `pending` y plazo. El `Clock` del armazón se inyecta cuando exista (VIG-320); hasta entonces se
 * usa el reloj del sistema, el mismo que usan `ports.base` y `state.query`.
 */
import { useParams } from 'react-router';

import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { systemClock } from '@/state/clock';

import { OcclusionScreen } from './OcclusionScreen';

export default function Page() {
  const zoneId = useParams().zone_id;
  if (zoneId === undefined || zoneId === '') return <NotAvailableInScope />;
  return <OcclusionScreen zoneId={zoneId} clock={systemClock} />;
}
