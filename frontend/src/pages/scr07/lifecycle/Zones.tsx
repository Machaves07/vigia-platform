/**
 * Asignar zona (`POST /nodes/{node_id}/zones`) y retirarla con motivo
 * (`POST /nodes/{node_id}/zones/{zone_id}/unassignment`). `fleet_zone_already_served` y
 * `fleet_zone_in_other_plant` se anclan a la zona; el servidor decide (BR-APP-43).
 */
import { useId, useState } from 'react';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import {
  fleetNotice,
  fleetPort,
  NODE_REASON_LIMITS,
  type FleetNode,
  type HierarchyPlant,
} from '@/ports/scr07Fleet';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { FleetNotice } from '../shared';
import { REASON_ANCHORS } from './Confirmed';

const ZONE_ANCHORS = {
  fleet_zone_already_served: 'zone',
  fleet_zone_in_other_plant: 'zone',
};

function zoneName(plant: HierarchyPlant | undefined, zoneId: string): string {
  const zone = plant?.zones.find((item) => item.zone_id === zoneId);
  return zone === undefined ? zoneId : `${zone.name} (${zone.code})`;
}

function AssignZone({
  node,
  plant,
}: {
  readonly node: FleetNode;
  readonly plant: HierarchyPlant | undefined;
}) {
  const id = useId();
  const [zoneId, setZoneId] = useState('');
  const [attempted, setAttempted] = useState(false);
  const mutation = useScreenMutation({
    write: (zone: string) =>
      fleetPort.assignZone.write({ path: { node_id: node.node_id }, body: { zone_id: zone } }),
    invalidates: { screen: 'scr07', resources: ['nodes', 'hierarchy'] },
  });
  const notice = fleetNotice(mutation.error, ZONE_ANCHORS);
  const options = (plant?.zones ?? []).filter((zone) => !node.zones.includes(zone.zone_id));
  return (
    <form
      aria-label={text('scr07.lifecycle.assign')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setAttempted(true);
        if (zoneId === '') return;
        void mutation.run(zoneId).catch(() => undefined);
      }}
    >
      <h3>{text('scr07.lifecycle.assign')}</h3>
      <FleetNotice notice={notice} field="form" id={`${id}-form`} />
      <label>
        {text('scr07.zone')}
        <select
          name="zone_id"
          required
          value={zoneId}
          aria-invalid={notice?.field === 'zone' || (attempted && zoneId === '')}
          aria-describedby={notice?.field === 'zone' ? `${id}-zone` : undefined}
          onChange={(event) => {
            mutation.reset();
            setZoneId(event.currentTarget.value);
          }}
        >
          <option value="">{text('scr07.lifecycle.choose_zone')}</option>
          {options.map((zone) => (
            <option key={zone.zone_id} value={zone.zone_id}>
              {`${zone.name} (${zone.code})`}
            </option>
          ))}
        </select>
      </label>
      <FleetNotice notice={notice} field="zone" id={`${id}-zone`} />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr07.lifecycle.assign_submit')}
      </button>
      {mutation.status === 'success' && <p role="status">{text('scr07.lifecycle.assigned')}</p>}
    </form>
  );
}

function UnassignZone({
  node,
  plant,
}: {
  readonly node: FleetNode;
  readonly plant: HierarchyPlant | undefined;
}) {
  const id = useId();
  const [zoneId, setZoneId] = useState('');
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);
  const mutation = useScreenMutation({
    write: (input: { zoneId: string; reason: string }) =>
      fleetPort.unassignZone.write({
        path: { node_id: node.node_id, zone_id: input.zoneId },
        body: { reason_es: input.reason },
      }),
    invalidates: { screen: 'scr07', resources: ['nodes', 'hierarchy'] },
  });
  const notice = fleetNotice(mutation.error, REASON_ANCHORS);
  return (
    <form
      aria-label={text('scr07.lifecycle.unassign')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setAttempted(true);
        if (zoneId === '' || reasonIssueKey(reason, NODE_REASON_LIMITS) !== null) return;
        void mutation.run({ zoneId, reason: reason.normalize('NFC') }).catch(() => undefined);
      }}
    >
      <h3>{text('scr07.lifecycle.unassign')}</h3>
      <FleetNotice notice={notice} field="form" id={`${id}-form`} />
      <label>
        {text('scr07.zone')}
        <select
          name="zone_id"
          required
          value={zoneId}
          aria-invalid={attempted && zoneId === ''}
          onChange={(event) => {
            mutation.reset();
            setZoneId(event.currentTarget.value);
          }}
        >
          <option value="">{text('scr07.lifecycle.choose_zone')}</option>
          {node.zones.map((zone) => (
            <option key={zone} value={zone}>
              {zoneName(plant, zone)}
            </option>
          ))}
        </select>
      </label>
      <ReasonField
        name="reason_es"
        label={text('scr07.lifecycle.reason')}
        value={reason}
        limits={NODE_REASON_LIMITS}
        showErrors={attempted}
        notice={notice?.field === 'reason' ? notice : undefined}
        onChange={(value) => {
          mutation.reset();
          setReason(value);
        }}
      />
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr07.lifecycle.unassign_submit')}
      </button>
      {mutation.status === 'success' && <p role="status">{text('scr07.lifecycle.unassigned')}</p>}
    </form>
  );
}

export function Zones({
  node,
  plant,
}: {
  readonly node: FleetNode;
  readonly plant: HierarchyPlant | undefined;
}) {
  return (
    <section aria-label={text('scr07.lifecycle.zones')}>
      <h2>{text('scr07.lifecycle.zones')}</h2>
      {node.zones.length === 0 ? (
        <p>{text('scr07.lifecycle.no_zones')}</p>
      ) : (
        <ul>
          {node.zones.map((zone) => (
            <li key={zone}>{zoneName(plant, zone)}</li>
          ))}
        </ul>
      )}
      <AssignZone node={node} plant={plant} />
      <UnassignZone node={node} plant={plant} />
    </section>
  );
}
