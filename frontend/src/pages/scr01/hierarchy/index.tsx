/** SCR-01: jerarquía de la sesión, sin elegir organización ni región de datos. */
import { useState } from 'react';
import { hierarchyPort } from '@/ports/scr01Hierarchy';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { HierarchyView } from './HierarchyView';
import { HierarchyNav, HierarchyRefresh, HierarchyResult, useHierarchyScope } from './shared';
import { ZoneForm } from './ZoneForm';
import { LoopConfiguration } from './LoopConfiguration';
import { PlantForm } from './PlantForm';
import styles from '../scr01.module.css';

export default function Page() {
  const context = useHierarchyScope();
  const [opened, setOpened] = useState<string | null>(null);
  const operation = 'GET /hierarchy';
  const query = useScreenQuery({
    key: screenKey({ screen: 'scr01', operation, ...context }),
    policy: policyFor(operation),
    read: ({ signal }) => hierarchyPort.hierarchy.read({}, signal),
  });
  return (
    <main className={styles.page}>
      <HierarchyNav />
      <h1>{text('scr01_hierarchy.title')}</h1>
      <HierarchyRefresh query={query} operation={operation} />
      <HierarchyResult query={query}>
        {query.data !== undefined && (
          <HierarchyView
            data={query.data}
            renderActions={(plant) => (
              <>
                <ZoneForm plantId={plant.plant_id} />
                <button
                  type="button"
                  className={styles.button}
                  aria-expanded={opened === plant.plant_id}
                  onClick={() => {
                    setOpened(opened === plant.plant_id ? null : plant.plant_id);
                  }}
                >
                  {text('scr01_hierarchy.loop_configuration')}
                </button>
                {opened === plant.plant_id && (
                  <LoopConfiguration key={plant.plant_id} plantId={plant.plant_id} />
                )}
              </>
            )}
          />
        )}
      </HierarchyResult>
      <PlantForm />
    </main>
  );
}
