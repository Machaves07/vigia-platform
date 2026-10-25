/**
 * SCR-05, grupo `gates`: `/plants/{plant_id}/gates` (compuertas de cada zona y política de
 * firmantes) y `/zones/{zone_id}/gates` (compuertas, acta, acuerdo, firmas y revocación).
 */
import { useParams } from 'react-router';
import { ScreenState } from '@/pages/shared/ScreenState';
import { PlantGates } from './PlantGates';
import { ZoneGates } from './ZoneGates';

export default function Page() {
  const { plant_id, zone_id } = useParams();
  if (zone_id !== undefined) return <ZoneGates key={zone_id} zoneId={zone_id} />;
  if (plant_id !== undefined) return <PlantGates key={plant_id} plantId={plant_id} />;
  return <ScreenState state="not_available_in_scope" />;
}
