/**
 * Grupo `record` de SCR-06 (IF-13 paso 7): el acta de comisionamiento cerrada y su documento a
 * demanda. El `Clock` del armazón se inyecta cuando exista (VIG-320); hasta entonces se usa el
 * reloj del sistema, el mismo que usan `ports.base` y `state.query`.
 */
import { useParams } from 'react-router';

import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { systemClock } from '@/state/clock';

import { RecordScreen } from './RecordScreen';

export default function Page() {
  const recordId = useParams().record_id;
  if (recordId === undefined || recordId === '') return <NotAvailableInScope />;
  return <RecordScreen key={recordId} recordId={recordId} clock={systemClock} />;
}
