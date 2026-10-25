/** Resumen de planta con su permiso; sin él, los vencidos y escalamientos del propio lazo. */
import { useState } from 'react';
import { Link } from 'react-router';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  closurePort,
  recordContent,
  stringField,
  type Detail,
  type LedgerRecord,
  type OverduePage,
} from '@/ports/scr09Closure';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { Failure, type ClosureContext } from './common';

export function OverdueView({
  page,
  timeZone,
  zoneNames = {},
}: {
  readonly page: OverduePage;
  readonly timeZone: string;
  readonly zoneNames?: Readonly<Record<string, string>>;
}) {
  return (
    <>
      <ul>
        {page.zones.map((zone) => (
          <li key={zone.zone_id}>
            {text('scr09.overdue.zone')}:{' '}
            {zoneNames[zone.zone_id] ?? text('scr09.overdue.zone_unknown')} ·{' '}
            {text('scr09.overdue.count')}: {zone.overdue_count} ·{' '}
            {text('scr09.overdue.highlighted')}: {zone.highlighted_count}
          </li>
        ))}
      </ul>
      <ul>
        {page.items.map((item) => (
          <li key={item.action_id}>
            <Link to={`/findings/${item.finding_state_id}/loop`}>{text('scr09.closure.back')}</Link>
            {' · '}
            {label('finding_lifecycle_state', item.lifecycle_state)}
            {' · '}
            <TimestampWithOffset instant={item.due_at} timeZone={timeZone} />
            {' · '}
            {text('scr09.overdue.days')}: {item.overdue_days}
            {item.highlighted && <strong> · {text('scr09.overdue.highlighted')}</strong>}
          </li>
        ))}
      </ul>
    </>
  );
}
export function Overdue({
  context,
  detail,
  timeZone,
  records,
}: {
  readonly context: ClosureContext;
  readonly detail: Detail;
  readonly timeZone: string;
  readonly records: readonly LedgerRecord[];
}) {
  const executive = context.permissions.includes('executive_summary.read');
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<OverduePage[]>([]);
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr09',
      operation: 'GET /loop/overdue',
      scope: context.scope,
      concession: context.concession,
      params: { plant_id: detail.plant_id, cursor },
    }),
    policy: policyFor('GET /loop/overdue'),
    enabled: executive,
    read: ({ signal }) =>
      closurePort.overdue.read(
        { query: { plant_id: detail.plant_id, limit: 50, ...(cursor === null ? {} : { cursor }) } },
        signal,
      ),
  });
  // Se muestran todos los vencidos del lazo: no hay filtros de estado, periodo ni clasificación.
  const local = detail.actions.filter((action) => action.counts_as_overdue);
  const zoneNames = Object.fromEntries(
    context.plants.flatMap((plant) => plant.zones.map((zone) => [zone.zone_id, zone.name])),
  );
  return (
    <section id="overdue" aria-label={text('scr09.overdue.title')}>
      <h2>{text('scr09.overdue.title')}</h2>
      {executive && (
        <>
          <Failure error={query.error} />
          {[...previous, ...(query.data === undefined ? [] : [query.data])].map((page, index) => (
            <OverdueView key={index} page={page} timeZone={timeZone} zoneNames={zoneNames} />
          ))}
          {query.data?.next_cursor != null && (
            <button
              type="button"
              disabled={query.isFetching}
              onClick={() => {
                if (query.data === undefined) return;
                setPrevious([...previous, query.data]);
                setCursor(query.data.next_cursor);
              }}
            >
              {text('scr09.overdue.more')}
            </button>
          )}
        </>
      )}
      <>
        <p>{text('scr09.overdue.local')}</p>
        <p>
          {text('scr09.overdue.zone')}:{' '}
          {context.plants
            .flatMap((plant) => plant.zones)
            .find((zone) => zone.zone_id === detail.zone_id)?.name ??
            text('scr09.overdue.zone_unknown')}
        </p>
        <p>
          {text('scr09.overdue.count')}: {local.length}
        </p>
        {local.map((action) => (
          <p key={action.action_id}>
            {action.action_description} ·{' '}
            <TimestampWithOffset instant={action.due_at} timeZone={timeZone} />
          </p>
        ))}
        <ul>
          {records
            .filter((record) => record.record_type === 'action_overdue')
            .map((record) => (
              <li key={record.record_id}>
                <TimestampWithOffset instant={record.received_at} timeZone={timeZone} /> ·{' '}
                {text('scr09.closure.record.action_overdue')} ·{' '}
                {label('escalation_level', stringField(recordContent(record).escalation_level))}
              </li>
            ))}
        </ul>
      </>
    </section>
  );
}
