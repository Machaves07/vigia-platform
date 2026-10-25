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

function actionState(state: string): string {
  return text(
    ACTION_STATES.includes(state) ? `scr09.loop.action_state.${state}` : 'scr09.loop.unknown',
  );
}

function ActionEntry({
  action,
  timeZone,
}: {
  readonly action: ActionItem;
  readonly timeZone: string;
}) {
  return (
    <li data-part="action">
      <p>
        <strong>{label('action_type', action.action_type)}</strong>
        {' · '}
        {text('scr09.loop.action_state')}: {actionState(action.state)}
        {action.counts_as_overdue && <> · {text('scr09.loop.counts_as_overdue')}</>}
      </p>
      <p>{action.action_description}</p>
      <p>{action.expected_outcome ?? text('scr09.loop.expected_none')}</p>
      <p>
        {text('scr09.loop.assigned_at')}:{' '}
        <TimestampWithOffset instant={action.assigned_at} timeZone={timeZone} />
        {' · '}
        {text('scr09.loop.due_at')}:{' '}
        <TimestampWithOffset instant={action.due_at} timeZone={timeZone} />
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
              <ActionEntry key={action.action_id} action={action} timeZone={timeZone} />
            ))}
          </ol>
        )}
      </section>
    </>
  );
}
