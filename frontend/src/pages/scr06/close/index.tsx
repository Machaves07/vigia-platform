/**
 * Grupo `close` de SCR-06 (IF-13 paso 6): cierre del acta con firmas, aceptación de falsas alarmas
 * y mediciones del instalador. Firma la persona de la sesión (`Principal`, instantánea de UI-16).
 * El `Clock` del armazón se inyecta cuando exista (VIG-320); hasta entonces se usa el reloj del
 * sistema, el mismo que usan `ports.base` y `state.query`.
 */
import { useParams } from 'react-router';

import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { systemClock } from '@/state/clock';
import { useUiSession } from '@/state/session';

import { CloseScreen, type CloseSigner } from './CloseScreen';

export default function Page() {
  const zoneId = useParams().zone_id;
  const { principal } = useUiSession();
  if (zoneId === undefined || zoneId === '') return <NotAvailableInScope />;
  const signer: CloseSigner | null =
    principal === null
      ? null
      : {
          user_id: principal.user_id,
          display_name: principal.display_name,
          professional_license: principal.professional_license ?? null,
        };
  return <CloseScreen zoneId={zoneId} clock={systemClock} signer={signer} />;
}
