import { useState } from 'react';
import { Link } from 'react-router';
import { fleetPort } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';
import { FleetNav, FleetRefresh, FleetResult, useFleetContext } from '../shared';
import { FleetTime, FleetView } from './FleetView';

export function NodeDetail({ nodeId }: { readonly nodeId: string }) {
  const context = useFleetContext();
  const [after, setAfter] = useState<string | null>(null);
  const operation = 'GET /fleet/nodes/{node_id}';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: { node_id: nodeId, history_after: after },
    }),
    policy: policyFor(operation),
    read: ({ signal }) =>
      fleetPort.detail.read(
        { path: { node_id: nodeId }, query: { history_after: after, history_limit: 50 } },
        signal,
      ),
  });
  const data = query.data;
  const timeZone =
    context.plants.find((plant) => plant.plant_id === data?.node.plant_id)?.timezone ?? '';
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.detail')}</h1>
      <p>
        <Link to={`/fleet/nodes/${nodeId}/lifecycle`}>{text('scr07.lifecycle.title')}</Link>
        {' · '}
        <Link to={`/fleet/nodes/${nodeId}/enrollment`}>{text('scr07.enrollment.title')}</Link>
      </p>
      <FleetRefresh query={query} operation={operation} />
      <FleetResult query={query} empty={false}>
        {data !== undefined && (
          <>
            <FleetView nodes={[data.node]} plants={context.plants} />
            <h2>{text('scr07.history')}</h2>
            {data.heartbeat_history.length === 0 ? (
              <ScreenState state="empty" message={text('scr07.history_empty')} />
            ) : (
              <table>
                <caption>{text('scr07.history')}</caption>
                <thead>
                  <tr>
                    <th scope="col">{text('scr07.received')}</th>
                    <th scope="col">{text('scr07.sent')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.heartbeat_history.map((heartbeat) => (
                    <tr key={heartbeat.heartbeat_id}>
                      <td>
                        <FleetTime instant={heartbeat.received_at} timeZone={timeZone} />
                      </td>
                      <td>
                        <FleetTime instant={heartbeat.sent_at} timeZone={timeZone} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {data.next_history_after !== null && (
              <button
                type="button"
                onClick={() => {
                  setAfter(data.next_history_after);
                }}
              >
                {text('scr07.load_more')}
              </button>
            )}
          </>
        )}
      </FleetResult>
    </main>
  );
}
