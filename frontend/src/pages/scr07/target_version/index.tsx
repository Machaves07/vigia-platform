/**
 * SCR-07, grupo `target_version` (IF-14 paso 5; A-61; VIG-325): planta, nodos de la planta o el
 * grupo `"plant"` (todos los nodos no revocados ni dados de baja, que resuelve el servidor),
 * versión y ventana de mantenimiento informativa (D-5) → `POST /fleet/target-versions` con
 * `plant_id` en el cuerpo. `fleet_version_outside_contract_window` se ancla a la versión; el
 * resultado de la actualización (aplicada o revertida) se lee del inventario.
 */
import { useId, useState } from 'react';
import { Link } from 'react-router';
import {
  fleetNotice,
  fleetPort,
  type TargetVersionBody,
  type TargetVersionPublication,
} from '@/ports/scr07Fleet';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { FleetTime } from '../inventory/FleetView';
import { FleetNav, FleetNotice, plantInstant, useFleetContext } from '../shared';

/** `maxItems` de `TargetVersionBody.node_ids`. */
const MAX_NODES = 100;
const ANCHORS = { fleet_version_outside_contract_window: 'target_version' };

type Target = 'plant' | 'nodes';

export default function Page() {
  const id = useId();
  const context = useFleetContext();
  const [plantId, setPlantId] = useState('');
  const [target, setTarget] = useState<Target>('plant');
  const [nodeIds, setNodeIds] = useState<readonly string[]>([]);
  const [version, setVersion] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [attempted, setAttempted] = useState(false);
  const [published, setPublished] = useState<TargetVersionPublication | null>(null);
  const mutation = useScreenMutation({
    write: (body: TargetVersionBody) => fleetPort.publishTargetVersion.write({ body }),
    invalidates: { screen: 'scr07', resources: ['nodes', 'fleet_alarms'] },
  });
  const notice = fleetNotice(mutation.error, ANCHORS);
  const plant = context.plants.find((item) => item.plant_id === plantId);
  const timeZone = plant?.timezone ?? '';
  const nodes = context.nodes.filter((node) => node.plant_id === plantId);
  const windowFrom = plantInstant(from, timeZone);
  const windowTo = plantInstant(to, timeZone);
  const windowInvalid = windowFrom === null || windowTo === null || windowTo <= windowFrom;
  const nodesInvalid = target === 'nodes' && (nodeIds.length === 0 || nodeIds.length > MAX_NODES);
  const edit =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      mutation.reset();
      setPublished(null);
      set(value);
    };
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.target_version.title')}</h1>
      <p>{text('scr07.target_version.guide')}</p>
      <form
        aria-label={text('scr07.target_version.title')}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          setAttempted(true);
          if (plantId === '' || version.trim() === '' || nodesInvalid || windowInvalid) return;
          const scope: Pick<TargetVersionBody, 'group' | 'node_ids'> =
            target === 'plant' ? { group: 'plant' } : { node_ids: [...nodeIds] };
          void mutation
            .run({
              plant_id: plantId,
              ...scope,
              target_version: version.trim(),
              maintenance_window: { from: windowFrom, to: windowTo },
            })
            .then(setPublished, () => undefined);
        }}
      >
        <FleetNotice notice={notice} field="form" id={`${id}-form`} />
        <label>
          {text('scr07.plant')}
          <select
            name="plant_id"
            required
            value={plantId}
            aria-invalid={attempted && plantId === ''}
            onChange={(event) => {
              const value = event.currentTarget.value;
              edit(setPlantId)(value);
              setNodeIds([]);
            }}
          >
            <option value="">{text('scr07.declare.choose_plant')}</option>
            {context.plants.map((item) => (
              <option key={item.plant_id} value={item.plant_id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>{text('scr07.target_version.scope')}</legend>
          <label>
            <input
              type="radio"
              name="target"
              value="plant"
              checked={target === 'plant'}
              onChange={() => {
                edit(setTarget)('plant');
              }}
            />
            {text('scr07.target_version.group_plant')}
          </label>
          <label>
            <input
              type="radio"
              name="target"
              value="nodes"
              checked={target === 'nodes'}
              onChange={() => {
                edit(setTarget)('nodes');
              }}
            />
            {text('scr07.target_version.some_nodes')}
          </label>
        </fieldset>
        {target === 'nodes' && (
          <fieldset aria-invalid={attempted && nodesInvalid}>
            <legend>{text('scr07.target_version.nodes')}</legend>
            {nodes.length === 0 ? (
              <p>{text('scr07.target_version.no_nodes')}</p>
            ) : (
              nodes.map((node) => (
                <label key={node.node_id}>
                  <input
                    type="checkbox"
                    name="node_ids"
                    value={node.node_id}
                    checked={nodeIds.includes(node.node_id)}
                    onChange={(event) => {
                      const checked = event.currentTarget.checked;
                      edit(setNodeIds)(
                        checked
                          ? [...nodeIds, node.node_id]
                          : nodeIds.filter((item) => item !== node.node_id),
                      );
                    }}
                  />
                  {node.code}
                </label>
              ))
            )}
            {attempted && nodesInvalid && (
              <p role="alert">{text('scr07.target_version.nodes_required')}</p>
            )}
          </fieldset>
        )}
        <label>
          {text('scr07.target')}
          <input
            name="target_version"
            required
            maxLength={64}
            value={version}
            aria-invalid={
              notice?.field === 'target_version' || (attempted && version.trim() === '')
            }
            aria-describedby={notice?.field === 'target_version' ? `${id}-version` : undefined}
            onChange={(event) => {
              edit(setVersion)(event.currentTarget.value);
            }}
          />
        </label>
        <FleetNotice notice={notice} field="target_version" id={`${id}-version`} />
        <fieldset aria-invalid={attempted && windowInvalid}>
          <legend>{text('scr07.target_version.window')}</legend>
          <p>{text('scr07.target_version.window_guide')}</p>
          <label>
            {text('scr07.target_version.from')}
            <input
              type="datetime-local"
              name="from"
              required
              value={from}
              onChange={(event) => {
                edit(setFrom)(event.currentTarget.value);
              }}
            />
          </label>
          <label>
            {text('scr07.target_version.to')}
            <input
              type="datetime-local"
              name="to"
              required
              value={to}
              onChange={(event) => {
                edit(setTo)(event.currentTarget.value);
              }}
            />
          </label>
          {attempted && windowInvalid && (
            <p role="alert">{text('scr07.target_version.window_invalid')}</p>
          )}
        </fieldset>
        <button type="submit" disabled={mutation.status === 'pending'}>
          {text('scr07.target_version.submit')}
        </button>
      </form>
      {published !== null && (
        <section aria-label={text('scr07.target_version.published')}>
          <p role="status">{text('scr07.target_version.published')}</p>
          <dl>
            <dt>{text('scr07.target')}</dt>
            <dd>{published.target_version}</dd>
            <dt>{text('scr07.target_version.node_count')}</dt>
            <dd>{published.node_ids.length}</dd>
            <dt>{text('scr07.target_version.published_at')}</dt>
            <dd>
              <FleetTime instant={published.published_at} timeZone={timeZone} />
            </dd>
          </dl>
          <p>
            {text('scr07.target_version.result_in_inventory')}{' '}
            <Link to="/fleet">{text('scr07.title')}</Link>
          </p>
        </section>
      )}
    </main>
  );
}
