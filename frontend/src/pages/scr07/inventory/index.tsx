/**
 * Inventario completo con filtros enviados en bloque y refresco gobernado por state.query. Cuando
 * es el inicio de la sesión (el administrador, BR-APP-04), va debajo de «Pendiente de su parte» y
 * de las áreas de configuración (`AdminHome`, VIG-365), con una sola actualización en la cabecera.
 */
import { useState } from 'react';
import { useParams } from 'react-router';
import { useIsHome } from '@/pages/shared/HomeCards';
import { PageRefresh } from '@/pages/shared/PageRefresh';
import { readFleet, type NodeFilters } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { descriptor } from '../descriptor';
import { FleetNav, FleetRefresh, FleetResult, useFleetContext } from '../shared';
import { AdminHome } from './AdminHome';
import { COMMUNICATION, FleetView, WARNINGS, warningLabel } from './FleetView';
import { NodeDetail } from './NodeDetail';
import { Thresholds } from './Thresholds';
import styles from './inventory.module.css';

function Inventory() {
  const context = useFleetContext();
  const isHome = useIsHome(descriptor, 'inventory');
  const [filters, setFilters] = useState<NodeFilters>({});
  const operation = 'GET /fleet/nodes';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation,
      scope: `${context.scope}:${filters.plant_id ?? 'all'}`,
      concession: context.concession,
      params: filters,
    }),
    policy: policyFor(operation),
    read: ({ signal }) => readFleet(filters, signal),
  });
  return (
    <main>
      {isHome ? (
        <>
          <header className={styles.header}>
            <h1>{text('scr07.home.title')}</h1>
            <PageRefresh screen="scr07" reads={[{ fetchedAt: query.fetchedAt, operation }]} />
          </header>
          <AdminHome context={context} />
          <h2>{text('scr07.home.fleet')}</h2>
          <FleetNav />
        </>
      ) : (
        <>
          <FleetNav />
          <h1>{text('scr07.title')}</h1>
        </>
      )}
      <form
        className={styles.filters}
        aria-label={text('scr07.filters')}
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const communication = COMMUNICATION.find((state) => state === data.get('communication'));
          const warning = WARNINGS.find((kind) => kind === data.get('warning'));
          const plant = data.get('plant');
          setFilters({
            plant_id: typeof plant === 'string' && plant !== '' ? plant : null,
            communication_state: communication ?? null,
            warning: warning ?? null,
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
          {text('scr07.communication')}
          <select name="communication">
            <option value="">{text('scr07.all')}</option>
            {COMMUNICATION.map((state) => (
              <option key={state} value={state}>
                {text(`scr07.communication.${state}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {text('scr07.warning')}
          <select name="warning">
            <option value="">{text('scr07.all')}</option>
            {WARNINGS.map((kind) => (
              <option key={kind} value={kind}>
                {warningLabel(kind)}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">{text('scr07.apply')}</button>
      </form>
      {!isHome && <FleetRefresh query={query} operation={operation} />}
      <FleetResult query={query} empty={query.data?.length === 0}>
        <FleetView nodes={query.data ?? []} plants={context.plants} />
      </FleetResult>
    </main>
  );
}

export default function Page() {
  const { node_id, plant_id } = useParams();
  if (node_id !== undefined) return <NodeDetail key={node_id} nodeId={node_id} />;
  if (plant_id !== undefined) return <Thresholds key={plant_id} plantId={plant_id} />;
  return <Inventory />;
}
