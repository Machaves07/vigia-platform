/**
 * UI-17 `GateStateChip`: estado de compuertas de una zona (BR-APP-31, 38), con el texto del
 * catálogo para cada `gate_display_state`. `agreement_revoked` muestra «Acuerdo revocado desde
 * <fecha>» en la zona horaria de la planta. Un valor fuera de la lista se muestra como «Estado
 * de compuertas desconocido», nunca con su valor crudo. Sin contadores ni color de estado.
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { text } from '@/texts';

import styles from './GateStateChip.module.css';

const GATE_DISPLAY_STATES = [
  'no_scope_record',
  'commissioning',
  'no_use_agreement',
  'regression_pending',
  'productive',
  'agreement_revoked',
] as const;

/** Lo que el chip necesita de `GateDisplay` (domain-entities §2.17). */
export interface GateStateChipProps {
  readonly gate: {
    /** `gate_display_state`. */
    readonly state: string;
    readonly agreement_revoked_at?: string | null;
  };
  /** Zona horaria IANA de la planta. */
  readonly timeZone: string;
}

function isGateDisplayState(state: string): state is (typeof GATE_DISPLAY_STATES)[number] {
  return (GATE_DISPLAY_STATES as readonly string[]).includes(state);
}

function Label({ gate, timeZone }: GateStateChipProps) {
  const { state, agreement_revoked_at: revokedAt } = gate;
  if (!isGateDisplayState(state)) return <>{text('components_status.gate.unknown')}</>;
  if (state !== 'agreement_revoked') return <>{text(`components_status.gate.${state}`)}</>;
  if (revokedAt === undefined || revokedAt === null) {
    return <>{text('components_status.gate.agreement_revoked_without_date')}</>;
  }
  return (
    <>
      {text('components_status.gate.agreement_revoked')}{' '}
      <TimestampWithOffset instant={revokedAt} timeZone={timeZone} />
    </>
  );
}

export function GateStateChip({ gate, timeZone }: GateStateChipProps) {
  const known = isGateDisplayState(gate.state);
  return (
    <span
      className={styles.chip}
      data-state={known ? gate.state : 'unknown'}
      role="group"
      aria-label={text('components_status.gate.group_label')}
    >
      <Label gate={gate} timeZone={timeZone} />
    </span>
  );
}
