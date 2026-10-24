/**
 * UI-25 `TimestampWithOffset`: fecha y hora en la zona horaria de la planta con la zona visible
 * (BR-APP-40, PR-APP-11). Variante con `nodeTime`: la hora del nodo en la misma zona y su desfase
 * respecto del instante de la plataforma. Con «todas las plantas», `plantName` indica en cada
 * fila de qué planta es la hora. Un instante o una zona no válidos muestran «Fecha no
 * disponible», nunca la hora en otra zona.
 */
import { text } from '@/texts';

import { nodeOffset, plantTime } from './format';
import styles from './TimestampWithOffset.module.css';

export interface TimestampWithOffsetProps {
  /** Instante ISO 8601 (p. ej. `received_at`). */
  readonly instant: string;
  /** Zona horaria IANA de la planta. */
  readonly timeZone: string;
  /** Hora declarada por el nodo, ISO 8601. */
  readonly nodeTime?: string;
  /** Nombre de la planta, cuando la vista muestra todas las plantas. */
  readonly plantName?: string;
}

function ZonedTime({ instant, timeZone }: { instant: string; timeZone: string }) {
  const value = plantTime(instant, timeZone);
  if (value === null) {
    return (
      <span className={styles.unavailable}>{text('components_status.timestamp.unavailable')}</span>
    );
  }
  return (
    <>
      <time dateTime={value.iso}>{value.local}</time>{' '}
      <span className={styles.zone} data-part="zone">
        ({timeZone}, {value.offset})
      </span>
    </>
  );
}

export function TimestampWithOffset({
  instant,
  timeZone,
  nodeTime,
  plantName,
}: TimestampWithOffsetProps) {
  const offset = nodeTime === undefined ? null : nodeOffset(instant, nodeTime);
  return (
    <span className={styles.timestamp} data-part="timestamp">
      {plantName !== undefined && (
        <span className={styles.plant} data-part="plant">
          {text('components_status.timestamp.plant')}: {plantName}
        </span>
      )}
      <span data-part="platform">
        <ZonedTime instant={instant} timeZone={timeZone} />
      </span>
      {nodeTime !== undefined && (
        <span className={styles.node} data-part="node">
          {text('components_status.timestamp.node_time')}:{' '}
          <ZonedTime instant={nodeTime} timeZone={timeZone} />
          {offset !== null && (
            <span data-part="offset">
              {' '}
              · {text('components_status.timestamp.node_offset')} {offset}
            </span>
          )}
        </span>
      )}
    </span>
  );
}
