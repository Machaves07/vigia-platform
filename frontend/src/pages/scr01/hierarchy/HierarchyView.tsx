/** RV-03: solo los campos declarados de plantas, zonas y nodo vigente, sin selector de región. */
import type { ReactNode } from 'react';
import { ScreenState } from '@/pages/shared/ScreenState';
import type { Hierarchy, Plant } from '@/ports/scr01Hierarchy';
import { text } from '@/texts';

export function HierarchyView({
  data,
  renderActions,
}: {
  readonly data: Hierarchy;
  readonly renderActions?: (plant: Plant) => ReactNode;
}) {
  if (data.plants.length === 0)
    return <ScreenState state="empty" message={text('scr01_hierarchy.empty')} />;
  return (
    <ul>
      {data.plants.map((plant) => (
        <li key={plant.plant_id}>
          <h2>
            {plant.code} — {plant.name}
          </h2>
          <dl>
            <dt>{text('scr01_hierarchy.country')}</dt>
            <dd>{plant.country}</dd>
            <dt>{text('scr01_hierarchy.data_region')}</dt>
            <dd data-part="data-region">{plant.data_region}</dd>
            <dt>{text('scr01_hierarchy.timezone')}</dt>
            <dd>{plant.timezone}</dd>
            <dt>{text('scr01_hierarchy.status')}</dt>
            <dd>
              {text(
                plant.status === 'active' ? 'scr01_hierarchy.active' : 'scr01_hierarchy.unknown',
              )}
            </dd>
          </dl>
          <h3>{text('scr01_hierarchy.zones')}</h3>
          {plant.zones.length === 0 ? (
            <ScreenState state="empty" message={text('scr01_hierarchy.no_zones')} />
          ) : (
            <ul aria-label={text('scr01_hierarchy.zones')}>
              {plant.zones.map((zone) => (
                <li key={zone.zone_id}>
                  <span>
                    {zone.code} — {zone.name}
                  </span>
                  <p>
                    {text('scr01_hierarchy.current_node')}:{' '}
                    {zone.node_id === null
                      ? text('scr01_hierarchy.no_node')
                      : (data.nodes.find((node) => node.node_id === zone.node_id)?.code ??
                        zone.node_id)}
                  </p>
                </li>
              ))}
            </ul>
          )}
          {renderActions?.(plant)}
        </li>
      ))}
    </ul>
  );
}
