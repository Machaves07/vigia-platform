/**
 * SCR-05, `/plants/{plant_id}/gates` (IF-11 pasos 1 y 4): el estado de compuertas de cada zona de
 * la planta (UI-17), el aviso informativo de planta sin política de incerrables y la política de
 * firmantes, con su formulario para quien tiene `commissioning.run`.
 */
import { Link } from 'react-router';
import { gatesPort, type Zone } from '@/ports/scr05Gates';
import { ScreenState } from '@/pages/shared/ScreenState';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import {
  GatesRefresh,
  GatesResult,
  PlantPolicyNotice,
  usePlantPolicy,
  useGatesContext,
  type GatesContext,
} from '../shared';
import { GateStatePanel, SignatoryPolicyView } from '../views';
import { SignatoryPolicyForm } from './SignatoryPolicyForm';
import styles from '../scr05.module.css';

const GATES = 'GET /zones/{zone_id}/gates';
const SIGNATORIES = 'GET /plants/{plant_id}/signatory-policy';

function ZoneRow({
  context,
  zone,
  timeZone,
}: {
  readonly context: GatesContext;
  readonly zone: Zone;
  readonly timeZone: string;
}) {
  const gates = useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: GATES,
      scope: `${context.scope}:${zone.zone_id}`,
      concession: context.concession,
      params: { zone_id: zone.zone_id },
    }),
    policy: policyFor(GATES),
    read: ({ signal }) => gatesPort.gates.read({ path: { zone_id: zone.zone_id } }, signal),
  });
  return (
    <li data-part="zone_row">
      <h3>
        <Link to={`/zones/${zone.zone_id}/gates`}>{zone.name}</Link>
      </h3>
      <GatesRefresh query={gates} operation={GATES} />
      <GatesResult query={gates} empty={null}>
        {gates.data === undefined ? null : (
          <GateStatePanel gates={gates.data} timeZone={timeZone} />
        )}
      </GatesResult>
    </li>
  );
}

export function PlantGates({ plantId }: { readonly plantId: string }) {
  const context = useGatesContext();
  const plant = context.plants.find((entry) => entry.plant_id === plantId);
  const policy = usePlantPolicy(context, plantId);
  const signatories = useScreenQuery({
    key: screenKey({
      screen: 'scr05',
      operation: SIGNATORIES,
      scope: `${context.scope}:${plantId}`,
      concession: context.concession,
      params: { plant_id: plantId },
    }),
    policy: policyFor(SIGNATORIES),
    read: ({ signal }) => gatesPort.signatoryPolicy.read({ path: { plant_id: plantId } }, signal),
  });
  if (context.hierarchy.status === 'success' && plant === undefined)
    return <ScreenState state="not_available_in_scope" />;
  const timeZone = plant?.timezone ?? 'UTC';
  return (
    <main className={styles.page}>
      <h1>{text('navigation.scr05')}</h1>
      {plant === undefined ? null : (
        <p data-part="plant">
          {text('scr05.plant')} {plant.name}
        </p>
      )}
      <PlantPolicyNotice policy={policy.data} plantId={plantId} />
      <nav aria-label={text('scr05.navigation')}>
        <Link to={`/plants/${plantId}/policy`}>{text('scr05.policy.link')}</Link>
      </nav>
      <section className={styles.section} aria-labelledby="scr05-zones">
        <h2 id="scr05-zones">{text('scr05.gates.zones')}</h2>
        <GatesRefresh query={context.hierarchy} operation="GET /hierarchy" />
        <GatesResult
          query={context.hierarchy}
          empty={plant?.zones.length === 0 ? text('scr05.gates.no_zones') : null}
        >
          <ul className={styles.list}>
            {plant?.zones.map((zone) => (
              <ZoneRow key={zone.zone_id} context={context} zone={zone} timeZone={timeZone} />
            ))}
          </ul>
        </GatesResult>
      </section>
      <section className={styles.section} aria-labelledby="scr05-signatories">
        <h2 id="scr05-signatories">{text('scr05.signatories.title')}</h2>
        <GatesRefresh query={signatories} operation={SIGNATORIES} />
        <GatesResult query={signatories} empty={null}>
          {signatories.data === undefined ? null : (
            <SignatoryPolicyView policy={signatories.data} timeZone={timeZone} />
          )}
        </GatesResult>
        {context.can('commissioning.run') && signatories.data !== undefined ? (
          <SignatoryPolicyForm
            key={signatories.data.updated_at ?? 'none'}
            plantId={plantId}
            policy={signatories.data}
          />
        ) : null}
      </section>
    </main>
  );
}
