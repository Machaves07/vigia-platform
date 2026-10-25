/**
 * SCR-07, grupo `lifecycle` (IF-14 paso 4; VIG-325): zonas del nodo, revocación y baja con motivo
 * y confirmación explícita, y recaptura del encuadre (A-55).
 *
 * La baja solo se ofrece cuando el **servidor** dice que el nodo está revocado: el `status` del
 * detalle (`GET /fleet/nodes/{node_id}`) o la respuesta de la revocación. La pantalla no deduce
 * el estado; si aun así no procede, el servidor responde `fleet_node_not_revoked`.
 */
import { useState } from 'react';
import { useParams } from 'react-router';
import { fleetPort, REVOKED_STATUS } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { FleetTime } from '../inventory/FleetView';
import { FleetNav, FleetRefresh, FleetResult, useFleetContext } from '../shared';
import { ConfirmedReasonForm } from './Confirmed';
import { FramingRecapture } from './Framing';
import { Zones } from './Zones';

function NodeLifecycle({ nodeId }: { readonly nodeId: string }) {
  const context = useFleetContext();
  const operation = 'GET /fleet/nodes/{node_id}';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation,
      scope: context.scope,
      concession: context.concession,
      params: { node_id: nodeId, history_after: null, history_limit: 1 },
    }),
    policy: policyFor(operation),
    read: ({ signal }) =>
      fleetPort.detail.read(
        { path: { node_id: nodeId }, query: { history_after: null, history_limit: 1 } },
        signal,
      ),
  });
  const [revokedAt, setRevokedAt] = useState<string | null>(null);
  const [decommissionedAt, setDecommissionedAt] = useState<string | null>(null);
  const revoke = useScreenMutation({
    write: (reason: string) =>
      fleetPort.revoke.write({ path: { node_id: nodeId }, body: { reason_es: reason } }),
    invalidates: { screen: 'scr07', resources: ['nodes', 'hierarchy', 'fleet_alarms'] },
  });
  const decommission = useScreenMutation({
    write: (reason: string) =>
      fleetPort.decommission.write({ path: { node_id: nodeId }, body: { reason_es: reason } }),
    invalidates: { screen: 'scr07', resources: ['nodes', 'hierarchy', 'fleet_alarms'] },
  });
  const node = query.data?.node;
  const plant = context.plants.find((item) => item.plant_id === node?.plant_id);
  const timeZone = plant?.timezone ?? '';
  const revoked = revokedAt !== null || node?.status === REVOKED_STATUS;
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.lifecycle.title')}</h1>
      <FleetRefresh query={query} operation={operation} />
      <FleetResult query={query} empty={false}>
        {node !== undefined && (
          <>
            <p>
              {text('scr07.node')}: {node.code} · <code>{node.node_id}</code>
            </p>
            <Zones node={node} plant={plant} />
            <section aria-label={text('scr07.lifecycle.revoke')}>
              <ConfirmedReasonForm
                title={text('scr07.lifecycle.revoke')}
                confirmation={text('scr07.lifecycle.revoke_confirm')}
                submit={text('scr07.lifecycle.revoke_submit')}
                status={revoke.status}
                error={revoke.error}
                onEdit={revoke.reset}
                onSubmit={(reason) => {
                  void revoke.run(reason).then(
                    (result) => {
                      if (result.status === REVOKED_STATUS) setRevokedAt(result.revoked_at);
                    },
                    () => undefined,
                  );
                }}
              />
              {revokedAt !== null && (
                <p role="status">
                  {text('scr07.lifecycle.revoked')}{' '}
                  <FleetTime instant={revokedAt} timeZone={timeZone} />
                </p>
              )}
            </section>
            {revoked && (
              <section aria-label={text('scr07.lifecycle.decommission')}>
                <p>{text('scr07.lifecycle.decommission_guide')}</p>
                <ConfirmedReasonForm
                  title={text('scr07.lifecycle.decommission')}
                  confirmation={text('scr07.lifecycle.decommission_confirm')}
                  submit={text('scr07.lifecycle.decommission_submit')}
                  status={decommission.status}
                  error={decommission.error}
                  onEdit={decommission.reset}
                  onSubmit={(reason) => {
                    void decommission.run(reason).then(
                      (result) => {
                        setDecommissionedAt(result.decommissioned_at);
                      },
                      () => undefined,
                    );
                  }}
                />
                {decommissionedAt !== null && (
                  <p role="status">
                    {text('scr07.lifecycle.decommissioned')}{' '}
                    <FleetTime instant={decommissionedAt} timeZone={timeZone} />
                  </p>
                )}
              </section>
            )}
            <FramingRecapture node={node} plant={plant} />
          </>
        )}
      </FleetResult>
    </main>
  );
}

export default function Page() {
  const { node_id } = useParams();
  if (node_id === undefined) return null;
  return <NodeLifecycle key={node_id} nodeId={node_id} />;
}
