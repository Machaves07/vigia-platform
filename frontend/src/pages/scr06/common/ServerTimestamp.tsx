/**
 * Marca de tiempo del servidor (IF-13, BR-APP-40). RV-09 no trae la zona de la planta: se usa la
 * de la planta del alcance de la sesión (VIG-366, «hora de la planta en todas las pantallas») y
 * la zona se nombra siempre junto a la hora, así que nunca se presenta como local una hora de otra
 * zona; si el alcance mezcla zonas, la hora va en UTC y lo dice.
 */
import { usePlantTimeZone } from '@/app/AppShell';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';

import styles from './common.module.css';

export function ServerTimestamp({ instant }: { readonly instant: string }) {
  const timeZone = usePlantTimeZone();
  return (
    <span className={styles.instant} data-part="server_instant">
      <TimestampWithOffset instant={instant} timeZone={timeZone} />
    </span>
  );
}
