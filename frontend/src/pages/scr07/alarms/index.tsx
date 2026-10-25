/** Alarmas activas y recientes: la transición y su momento vienen de la plataforma. */
import { useState } from 'react';
import { Link } from 'react-router';
import { fleetPort, type AlarmFilters } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';
import { FleetNav, FleetRefresh, FleetResult, useFleetContext } from '../shared';
import { FleetTime, WARNINGS, warningLabel } from '../inventory/FleetView';
import styles from '../inventory/inventory.module.css';

export default function Page() {
  const context = useFleetContext();
  const [filters, setFilters] = useState<AlarmFilters>({});
  const [after, setAfter] = useState<string | null>(null);
  const operation = 'GET /fleet/alarms';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation,
      scope: `${context.scope}:${filters.plant_id ?? 'all'}`,
      concession: context.concession,
      params: { ...filters, after },
    }),
    policy: policyFor(operation),
    read: ({ signal }) =>
      fleetPort.alarms.read({ query: { ...filters, limit: 50, after } }, signal),
  });
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.alarms')}</h1>
      <form
        className={styles.filters}
        aria-label={text('scr07.filters')}
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const alarm_kind = WARNINGS.find((kind) => kind === data.get('kind')) ?? null;
          const status = data.get('status');
          const plant = data.get('plant');
          setAfter(null);
          setFilters({
            plant_id: typeof plant === 'string' && plant !== '' ? plant : null,
            alarm_kind,
            status: status === 'active' || status === 'cleared' ? status : null,
          });
        }}
      >
        <label>
          {text('scr07.plant')}
          <select name="plant">
            <option value="">{text('scr07.all')}</option>
            {context.plants.map((plant) => (
              <option key={plant.plant_id} value={plant.plant_id}>
                {plant.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {text('scr07.warning')}
          <select name="kind">
            <option value="">{text('scr07.all')}</option>
            {WARNINGS.map((kind) => (
              <option key={kind} value={kind}>
                {warningLabel(kind)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {text('scr07.alarm_status')}
          <select name="status">
            <option value="">{text('scr07.all')}</option>
            <option value="active">{text('scr07.active')}</option>
            <option value="cleared">{text('scr07.cleared')}</option>
          </select>
        </label>
        <button type="submit">{text('scr07.apply')}</button>
      </form>
      <FleetRefresh query={query} operation={operation} />
      <FleetResult query={query} empty={false}>
        {query.data?.alarms.length === 0 ? (
          <ScreenState state="empty" message={text('scr07.alarms_empty')} />
        ) : (
          <table>
            <caption>{text('scr07.alarms')}</caption>
            <thead>
              <tr>
                {[
                  'scr07.node',
                  'scr07.plant',
                  'scr07.zone',
                  'scr07.warning',
                  'scr07.raised',
                  'scr07.cleared',
                ].map((key) => (
                  <th key={key} scope="col">
                    {text(key)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {query.data?.alarms.map((alarm) => {
                const plant = context.plants.find((entry) => entry.plant_id === alarm.plant_id);
                return (
                  <tr key={alarm.alarm_id}>
                    <td>
                      <Link to={`/fleet/nodes/${alarm.node_id}`}>{alarm.node_id}</Link>
                    </td>
                    <td>{plant?.name ?? alarm.plant_id}</td>
                    <td>{alarm.zone_id ?? text('scr07.unknown')}</td>
                    <td>{warningLabel(alarm.alarm_kind)}</td>
                    <td>
                      <FleetTime instant={alarm.raised_at} timeZone={plant?.timezone ?? ''} />
                    </td>
                    <td>
                      {alarm.cleared_at === null ? (
                        text('scr07.active')
                      ) : (
                        <FleetTime instant={alarm.cleared_at} timeZone={plant?.timezone ?? ''} />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {query.data?.next_after != null && (
          <button
            type="button"
            onClick={() => {
              setAfter(query.data?.next_after ?? null);
            }}
          >
            {text('scr07.load_more')}
          </button>
        )}
      </FleetResult>
    </main>
  );
}
