/**
 * Señales de un tramo (O-2 paso a; TASK-587): rol y valor etiquetados, la marca de la lectura no
 * válida y la hora de lectura (UI-25) en la zona horaria de la planta, **tal como la da el
 * servidor**: en una zona unipersonal `read_at` ya llega agregado a la ventana (BR-LAZ-06) y se
 * dice. La consola no recalcula ni redondea la hora. Sin `read_at`, su ausencia.
 *
 * Compartido: lo usan la clasificación (SCR-09) y el detalle (SCR-08, TASK-584); sus textos van en
 * `signal_readings.json`, del armazón, para que ninguna de las dos cargue los de la otra.
 * Selección explícita de campos (BR-APP-40): nada del objeto recibido se serializa.
 *
 * Con `description_es` (TASK-595/596) la señal se lee por su nombre del catálogo y el rol queda
 * como texto secundario; sin él, por su rol como antes.
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { SignalReading } from '@/ports/shared';
import { text } from '@/texts';
import { label } from '@/texts/labels';

function ReadAt({
  readAt,
  timeZone,
  aggregated,
}: {
  readonly readAt: string | null;
  readonly timeZone: string;
  readonly aggregated: boolean;
}) {
  if (readAt === null) {
    return (
      <span data-part="signal_read_at_unavailable">
        {text('signal_readings.read_at_unavailable')}
      </span>
    );
  }
  return (
    <span data-part="signal_read_at">
      {text('signal_readings.read_at')}:{' '}
      <TimestampWithOffset instant={readAt} timeZone={timeZone} />
      {aggregated && (
        <>
          {' '}
          (<span data-part="signal_aggregated">{text('signal_readings.aggregated')}</span>)
        </>
      )}
    </span>
  );
}

export function SignalReadings({
  signals,
  name,
  timeZone,
  aggregated,
}: {
  readonly signals: readonly SignalReading[];
  /** Nombre accesible de la lista (p. ej. el tramo). */
  readonly name: string;
  readonly timeZone: string;
  /** `time_aggregated` del detalle: la hora llega agregada a la ventana de la zona. */
  readonly aggregated: boolean;
}) {
  if (signals.length === 0) {
    return <p data-part="no_signals">{text('signal_readings.none')}</p>;
  }
  return (
    <ul aria-label={name} data-part="signal_readings">
      {signals.map((signal, index) => (
        <li
          key={`${signal.role}:${String(index)}`}
          data-part="signal"
          data-valid={signal.valid ? 'true' : 'false'}
        >
          {signal.description_es === null ? (
            <span data-part="signal_role">{label('signal_role', signal.role)}</span>
          ) : (
            // Con su nombre del catálogo (TASK-596): «Jumbo en posición (rol): afirmada».
            <>
              <span data-part="signal_name">{signal.description_es}</span> (
              <span data-part="signal_role">{label('signal_role', signal.role)}</span>)
            </>
          )}
          : <span data-part="signal_value">{label('signal_value', signal.value)}</span>
          {!signal.valid && (
            <>
              {' '}
              (<span data-part="signal_invalid">{text('signal_readings.invalid')}</span>)
            </>
          )}
          {' · '}
          <ReadAt readAt={signal.read_at} timeZone={timeZone} aggregated={aggregated} />
        </li>
      ))}
    </ul>
  );
}
