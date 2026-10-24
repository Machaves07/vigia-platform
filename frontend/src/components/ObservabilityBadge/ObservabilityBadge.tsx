/**
 * UI-10 `ObservabilityBadge`: el semáforo de observabilidad (BR-APP-23, 35; PR-APP-08).
 *
 * Icono propio y texto del catálogo siempre; el color de `data-tone` solo refuerza. La causa
 * lleva su etiqueta neutral y la leyenda «Observable no significa zona despejada» acompaña al
 * semáforo en todo lugar, sin propiedad que la oculte. El nombre accesible del grupo dice el
 * estado con palabras, así que el semáforo se entiende sin color.
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { text } from '@/texts';

import { causeLabel, encodingFor, isKnownState } from './encoding';
import { EyeIcon } from './EyeIcon';
import styles from './ObservabilityBadge.module.css';

export interface ObservabilityBadgeProps {
  /** `observability_state`; cualquier otro valor se muestra como «Desconocido». */
  readonly state: string;
  /** `degradation_cause` (U-01) o `platform_cause` (U-02). */
  readonly cause?: string;
  /** Desde cuándo rige el estado, en la zona horaria de la planta. */
  readonly since?: { readonly instant: string; readonly timeZone: string };
}

export function ObservabilityBadge({ state, cause, since }: ObservabilityBadgeProps) {
  const encoding = encodingFor(state);
  const stateText = text(encoding.textKey);
  return (
    <div
      className={styles.badge}
      role="group"
      aria-label={`${text('components_status.observability.group_label')}: ${stateText}`}
      data-state={isKnownState(state) ? state : 'unknown'}
    >
      <span className={styles.chip} data-tone={encoding.tone}>
        <EyeIcon kind={encoding.icon} className={styles.icon} />
        <span data-part="state">{stateText}</span>
      </span>
      {cause !== undefined && (
        <span className={styles.detail} data-part="cause">
          {text('components_status.observability.cause')}:{' '}
          {causeLabel(cause) ?? text('components_status.observability.cause_unknown')}
        </span>
      )}
      {since !== undefined && (
        <span className={styles.detail} data-part="since">
          {text('components_status.observability.since')}{' '}
          <TimestampWithOffset instant={since.instant} timeZone={since.timeZone} />
        </span>
      )}
      <p className={styles.legend} data-part="legend">
        {text('components_status.observability.legend')}
      </p>
    </div>
  );
}
