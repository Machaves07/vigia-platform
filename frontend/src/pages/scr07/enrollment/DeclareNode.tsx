/**
 * Declarar nodo (IF-14 paso 2): código, zonas de la planta y `replaces_node_id` opcional →
 * `POST /plants/{plant_id}/nodes`. Validación estructural; `fleet_code_in_use`,
 * `fleet_zone_already_served`, `fleet_zone_in_other_plant` y `fleet_replaced_node_not_found` se
 * anclan a su campo. El servidor decide si el nodo se declara (BR-APP-43).
 */
import { useId, useState } from 'react';
import { Link } from 'react-router';
import {
  fleetNotice,
  fleetPort,
  type DeclarationBody,
  type DeclaredNode,
} from '@/ports/scr07Fleet';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { FleetNav, FleetNotice, useFleetContext } from '../shared';

/** Máximo de zonas de una declaración (`maxItems` de `NodeDeclarationBody.zone_ids`). */
const MAX_ZONES = 16;
const ANCHORS = {
  fleet_code_in_use: 'code',
  fleet_zone_already_served: 'zones',
  fleet_zone_in_other_plant: 'zones',
  fleet_replaced_node_not_found: 'replaces',
};

export function DeclareNode() {
  const id = useId();
  const context = useFleetContext();
  const [plantId, setPlantId] = useState('');
  const [code, setCode] = useState('');
  const [zones, setZones] = useState<readonly string[]>([]);
  const [replaces, setReplaces] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const [declared, setDeclared] = useState<DeclaredNode | null>(null);
  const mutation = useScreenMutation({
    write: (input: { plantId: string; body: DeclarationBody }) =>
      fleetPort.declare.write({ path: { plant_id: input.plantId }, body: input.body }),
    invalidates: { screen: 'scr07', resources: ['nodes', 'hierarchy'] },
  });
  const notice = fleetNotice(mutation.error, ANCHORS);
  const plant = context.plants.find((item) => item.plant_id === plantId);
  const candidates = context.nodes.filter((node) => node.plant_id === plantId);
  const codeMissing = code.trim() === '';
  const tooMany = zones.length > MAX_ZONES;
  const edit =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      mutation.reset();
      setDeclared(null);
      set(value);
    };
  const fieldError = (field: string, local: boolean) =>
    notice?.field === field || (showErrors && local);
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.declare.title')}</h1>
      <p>{text('scr07.declare.guide')}</p>
      <form
        aria-label={text('scr07.declare.title')}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          setShowErrors(true);
          if (plantId === '' || codeMissing || tooMany) return;
          void mutation
            .run({
              plantId,
              body: {
                code: code.trim(),
                zone_ids: [...zones],
                replaces_node_id: replaces === '' ? null : replaces,
              },
            })
            .then(setDeclared, () => undefined);
        }}
      >
        <FleetNotice notice={notice} field="form" id={`${id}-form`} />
        <label>
          {text('scr07.plant')}
          <select
            name="plant"
            required
            value={plantId}
            aria-invalid={showErrors && plantId === ''}
            onChange={(event) => {
              const value = event.currentTarget.value;
              edit(setPlantId)(value);
              setZones([]);
              setReplaces('');
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
        <label>
          {text('scr07.declare.code')}
          <input
            name="code"
            required
            value={code}
            aria-invalid={fieldError('code', codeMissing)}
            aria-describedby={notice?.field === 'code' ? `${id}-code` : undefined}
            onChange={(event) => {
              edit(setCode)(event.currentTarget.value);
            }}
          />
        </label>
        <FleetNotice notice={notice} field="code" id={`${id}-code`} />
        <fieldset
          aria-invalid={fieldError('zones', tooMany)}
          aria-describedby={notice?.field === 'zones' ? `${id}-zones` : undefined}
        >
          <legend>{text('scr07.declare.zones')}</legend>
          {plant === undefined || plant.zones.length === 0 ? (
            <p>{text('scr07.declare.no_zones')}</p>
          ) : (
            plant.zones.map((zone) => (
              <label key={zone.zone_id}>
                <input
                  type="checkbox"
                  name="zone_ids"
                  value={zone.zone_id}
                  checked={zones.includes(zone.zone_id)}
                  onChange={(event) => {
                    const checked = event.currentTarget.checked;
                    edit(setZones)(
                      checked
                        ? [...zones, zone.zone_id]
                        : zones.filter((item) => item !== zone.zone_id),
                    );
                  }}
                />
                {`${zone.name} (${zone.code})`}
              </label>
            ))
          )}
          {showErrors && tooMany && <p role="alert">{text('scr07.declare.too_many_zones')}</p>}
        </fieldset>
        <FleetNotice notice={notice} field="zones" id={`${id}-zones`} />
        <label>
          {text('scr07.declare.replaces')}
          <select
            name="replaces_node_id"
            value={replaces}
            aria-invalid={notice?.field === 'replaces'}
            aria-describedby={notice?.field === 'replaces' ? `${id}-replaces` : undefined}
            onChange={(event) => {
              edit(setReplaces)(event.currentTarget.value);
            }}
          >
            <option value="">{text('scr07.declare.no_replacement')}</option>
            {candidates.map((node) => (
              <option key={node.node_id} value={node.node_id}>
                {node.code}
              </option>
            ))}
          </select>
        </label>
        <FleetNotice notice={notice} field="replaces" id={`${id}-replaces`} />
        <button type="submit" disabled={mutation.status === 'pending'}>
          {text('scr07.declare.submit')}
        </button>
      </form>
      {declared !== null && (
        <section aria-label={text('scr07.declare.done')}>
          <p role="status">{text('scr07.declare.done')}</p>
          <dl>
            <dt>{text('scr07.node')}</dt>
            <dd>{declared.code}</dd>
            <dt>{text('components_forms.secret.node_id')}</dt>
            <dd>
              <code>{declared.node_id}</code>
            </dd>
          </dl>
          <Link to={`/fleet/nodes/${declared.node_id}/enrollment`}>
            {text('scr07.enrollment.go')}
          </Link>
        </section>
      )}
    </main>
  );
}
