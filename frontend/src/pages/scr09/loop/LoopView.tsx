/**
 * RV-13 (estado del lazo, registros y acción; SUP-U05-02): el estado con su etiqueta (los nueve
 * de `finding_lifecycle_state`; «desconocido» fuera de la lista), las clasificaciones y las
 * acciones en el orden del servidor, y la zona que nombra el escalamiento. Ningún dato de persona
 * observada ni botón de editar o borrar (BR-APP-24, 33). Selección explícita de campos.
 */
import type { ReactNode } from 'react';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { ActionItem, FindingDetail } from '@/ports/scr09Classify';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { ClassificationHistory } from '../classify/History';

const ACTION_STATES: readonly string[] = ['assigned', 'overdue', 'closed', 'superseded'];
/** Estados de la fila de la acción vigente (no sustituida) mientras el lazo sigue abierto. */
const LIVE_ACTION_STATES: readonly string[] = ['assigned', 'overdue'];

/**
 * El estado que se muestra de la acción. El cierre firmado (T09) cita la acción vigente y no
 * reescribe su fila, que sigue `assigned` u `overdue` (U-04, VIG-233; diagnóstico de VIG-363): con
 * el hallazgo `closed`, la acción vigente se presenta cerrada con el hallazgo.
 */
function shownActionState(action: ActionItem, lifecycleState: string): string {
  if (lifecycleState === 'closed' && LIVE_ACTION_STATES.includes(action.state)) return 'closed';
  return action.state;
}

function actionState(state: string): string {
  // El servidor declara el vencimiento; counts_as_overdue solo indica si entra en su cómputo.
  // No se deduce de due_at, que puede venir agregado por privacidad en zonas unipersonales.
  return text(
    ACTION_STATES.includes(state) ? `scr09.loop.action_state.${state}` : 'scr09.loop.unknown',
  );
}

function ActionEntry({
  action,
  lifecycleState,
  timeZone,
}: {
  readonly action: ActionItem;
  readonly lifecycleState: string;
  readonly timeZone: string;
}) {
  const shown = shownActionState(action, lifecycleState);
  return (
    <li data-part="action">
      <p>
        <strong>{label('action_type', action.action_type)}</strong>
        {' · '}
        {text('scr09.loop.action_state')}:{' '}
        <span data-part="action_state" data-value={shown}>
          {actionState(shown)}
        </span>
        {shown !== action.state && <> · {text('role_navigation.loop.closed_with_finding')}</>}
        {!action.counts_as_overdue && <> · {text('scr09.loop.overdue_count_suspended')}</>}
      </p>
      <p>{action.action_description}</p>
      {/* VIG-366, S3-25: el resultado esperado con su etiqueta y el plazo solo con la fecha. */}
      <p data-part="expected_outcome">
        {text('scr09.loop.expected_label')}:{' '}
        {action.expected_outcome ?? text('scr09.loop.expected_none')}
      </p>
      <p>
        {text('scr09.loop.assigned_at')}:{' '}
        <TimestampWithOffset instant={action.assigned_at} timeZone={timeZone} />
        {' · '}
        {text('scr09.loop.due_at')}:{' '}
        <TimestampWithOffset instant={action.due_at} timeZone={timeZone} dateOnly />
      </p>
    </li>
  );
}

export function LoopView({
  detail,
  zone,
  timeZone,
  children,
}: {
  readonly detail: FindingDetail;
  /** Nombre de la zona del hallazgo en la jerarquía; `null` si no se conoce. */
  readonly zone: string | null;
  readonly timeZone: string;
  /** Enlaces a los formularios de los registros siguientes. */
  readonly children?: ReactNode;
}) {
  return (
    <>
      <p>
        {text('scr09.loop.state')}:{' '}
        <strong data-part="lifecycle_state" data-value={detail.lifecycle_state}>
          {label('finding_lifecycle_state', detail.lifecycle_state)}
        </strong>
      </p>
      <p>
        {text('scr09.loop.zone')}: {zone ?? text('scr09.loop.unknown')}
      </p>
      {children}
      <section aria-label={text('scr09.loop.records')}>
        <h2>{text('scr09.loop.records')}</h2>
        <h3>{text('scr09.classify.history')}</h3>
        <ClassificationHistory
          classifications={detail.classifications}
          timeAggregated={detail.time_aggregated}
          timeZone={timeZone}
        />
        <h3>{text('scr09.loop.actions')}</h3>
        {detail.actions.length === 0 ? (
          <p>{text('scr09.loop.actions_empty')}</p>
        ) : (
          <ol aria-label={text('scr09.loop.actions')}>
            {detail.actions.map((action) => (
              <ActionEntry
                key={action.action_id}
                action={action}
                lifecycleState={detail.lifecycle_state}
                timeZone={timeZone}
              />
            ))}
          </ol>
        )}
      </section>
    </>
  );
}
