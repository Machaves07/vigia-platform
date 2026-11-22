/**
 * Señales de un tramo (O-2 paso a; TASK-587): rol y valor etiquetados, la marca de la lectura no
 * válida y la hora de lectura (UI-25) en la zona horaria de la planta, **tal como la da el
 * servidor**: en una zona unipersonal `read_at` ya llega agregado a la ventana (BR-LAZ-06) y se
 * dice. La consola no recalcula ni redondea la hora. Sin `read_at`, su ausencia.
 *
 * Compartido: lo usa la clasificación (SCR-09) y lo adopta el detalle (SCR-08) en TASK-584.
 * Selección explícita de campos (BR-APP-40): nada del objeto recibido se serializa.
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
        {text('scr09.classify.signal_read_at_unavailable')}
      </span>
    );
  }
  return (
    <span data-part="signal_read_at">
      {text('scr09.classify.signal_read_at')}:{' '}
      <TimestampWithOffset instant={readAt} timeZone={timeZone} />
      {aggregated && (
        <>
          {' '}
          (<span data-part="signal_aggregated">{text('scr09.classify.aggregated')}</span>)
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
    return <p data-part="no_signals">{text('scr09.classify.signals_record_none')}</p>;
  }
  return (
    <ul aria-label={name} data-part="signal_readings">
      {signals.map((signal, index) => (
        <li
          key={`${signal.role}:${String(index)}`}
          data-part="signal"
          data-valid={signal.valid ? 'true' : 'false'}
        >
          <span data-part="signal_role">{label('signal_role', signal.role)}</span>:{' '}
          <span data-part="signal_value">{label('signal_value', signal.value)}</span>
          {!signal.valid && (
            <>
              {' '}
              (<span data-part="signal_invalid">{text('scr09.classify.signal_invalid')}</span>)
            </>
          )}
          {' · '}
          <ReadAt readAt={signal.read_at} timeZone={timeZone} aggregated={aggregated} />
        </li>
      ))}
    </ul>
  );
}
