/**
 * UI-25 `TimestampWithOffset`: fecha y hora en la zona horaria de la planta con la zona visible
 * (BR-APP-40, PR-APP-11). Variante con `nodeTime`: la hora del nodo en la misma zona y su desfase
 * respecto del instante de la plataforma. Con «todas las plantas», `plantName` indica en cada
 * fila de qué planta es la hora. Un instante o una zona no válidos muestran «Fecha no
 * disponible», nunca la hora en otra zona.
 */
import { text } from '@/texts';

import { nodeOffset, plantTime, zoneName } from './format';
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
  /**
   * En una tabla la zona va una sola vez, en la cabecera (`ZoneHeader`; VIG-366): `'header'` la
   * quita de la celda. Solo cuando todas las filas son de la misma zona.
   */
  readonly zone?: 'inline' | 'header';
  /** Solo la fecha, sin la hora (un plazo que vence en un día; VIG-366, S3-25). */
  readonly dateOnly?: boolean;
}

function ZonedTime({
  instant,
  timeZone,
  inlineZone,
  dateOnly,
}: {
  instant: string;
  timeZone: string;
  inlineZone: boolean;
  dateOnly: boolean;
}) {
  const value = plantTime(instant, timeZone);
  if (value === null) {
    return (
      <span className={styles.unavailable}>{text('components_status.timestamp.unavailable')}</span>
    );
  }
  return (
    <>
      <time dateTime={value.iso}>{dateOnly ? value.local.slice(0, 10) : value.local}</time>
      {inlineZone && (
        <>
          {' '}
          <span className={styles.zone} data-part="zone">
            ({timeZone}, {value.offset})
          </span>
        </>
      )}
    </>
  );
}

/**
 * La zona de una columna de horas, una sola vez (VIG-366): «(hora de Colombia)». Las celdas usan
 * `zone="header"`. Una zona no válida no se nombra.
 */
export function ZoneHeader({ timeZone }: { readonly timeZone: string }) {
  const name = zoneName(timeZone);
  if (name === null) return null;
  return (
    <span className={styles.zone} data-part="zone_header">
      {' '}
      ({name})
    </span>
  );
}

export function TimestampWithOffset({
  instant,
  timeZone,
  nodeTime,
  plantName,
  zone = 'inline',
  dateOnly = false,
}: TimestampWithOffsetProps) {
  const inlineZone = zone === 'inline';
  const offset = nodeTime === undefined ? null : nodeOffset(instant, nodeTime);
  return (
    <span className={styles.timestamp} data-part="timestamp">
      {plantName !== undefined && (
        <span className={styles.plant} data-part="plant">
          {text('components_status.timestamp.plant')}: {plantName}
        </span>
      )}
      <span data-part="platform">
        <ZonedTime
          instant={instant}
          timeZone={timeZone}
          inlineZone={inlineZone}
          dateOnly={dateOnly}
        />
      </span>
      {nodeTime !== undefined && (
        <span className={styles.node} data-part="node">
          {text('components_status.timestamp.node_time')}:{' '}
          <ZonedTime
            instant={nodeTime}
            timeZone={timeZone}
            inlineZone={inlineZone}
            dateOnly={false}
          />
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
