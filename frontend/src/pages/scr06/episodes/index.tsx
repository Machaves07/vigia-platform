/**
 * Grupo `episodes` de SCR-06 (TASK-574; A-112, QF10): los episodios de la sombra de la zona
 * (`/zones/:zone_id/commissioning/episodes`) y el detalle de uno con su foto difuminada
 * (`…/episodes/:photo_id`). La guarda del enrutador ya exige `commissioning.run`; un parámetro
 * que no es un UUID no pide nada y pinta «no disponible en su alcance» (BR-APP-77). El `Clock`
 * del armazón se inyecta cuando exista (VIG-320); hasta entonces, el reloj del sistema.
 */
import { useParams } from 'react-router';

import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { systemClock } from '@/state/clock';

import { EpisodeDetailScreen } from './EpisodeDetailScreen';
import { EpisodesScreen } from './EpisodesScreen';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default function Page() {
  const { zone_id: zoneId = '', photo_id: photoId } = useParams();
  if (!UUID.test(zoneId)) return <NotAvailableInScope />;
  if (photoId === undefined) {
    return <EpisodesScreen key={zoneId} zoneId={zoneId} clock={systemClock} />;
  }
  if (!UUID.test(photoId)) return <NotAvailableInScope />;
  return (
    <EpisodeDetailScreen
      key={`${zoneId}:${photoId}`}
      zoneId={zoneId}
      photoId={photoId}
      clock={systemClock}
    />
  );
}
