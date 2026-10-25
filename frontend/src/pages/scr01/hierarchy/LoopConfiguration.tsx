/** SUP-U05-38/39: configuración y cambios por planta, con zona horaria informativa heredada. */
import { Fragment } from 'react';
import {
  hierarchyPort,
  LOOP_FIELDS,
  type LoopConfiguration as Configuration,
} from '@/ports/scr01Hierarchy';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { HierarchyRefresh, HierarchyResult, useHierarchyScope } from './shared';
import { LoopConfigurationForm } from './LoopConfigurationForm';

const WEEKDAYS = [
  'monday',
  'tuesday',
  'wednesday',
  'thursday',
  'friday',
  'saturday',
  'sunday',
] as const;

export function LoopConfigurationView({
  configuration,
}: {
  readonly configuration: Configuration;
}) {
  return (
    <>
      <dl>
        <dt>{text('scr01_hierarchy.timezone')}</dt>
        <dd>{configuration.timezone}</dd>
        {LOOP_FIELDS.map((field) => (
          <Fragment key={field}>
            <dt>{text(`scr01_hierarchy.${field}`)}</dt>
            <dd>{configuration[field]}</dd>
          </Fragment>
        ))}
      </dl>
      <h4>{text('scr01_hierarchy.working_weekdays')}</h4>
      <ul>
        {configuration.business_calendar.working_weekdays.map((day, index) => (
          <li key={index}>{text(`scr01_hierarchy.${WEEKDAYS[day - 1] ?? 'unknown'}`)}</li>
        ))}
      </ul>
      <h4>{text('scr01_hierarchy.non_working_dates')}</h4>
      {configuration.business_calendar.non_working_dates.length === 0 ? (
        <p>{text('scr01_hierarchy.no_dates')}</p>
      ) : (
        <ul>
          {configuration.business_calendar.non_working_dates.map((date, index) => (
            <li key={index}>{date}</li>
          ))}
        </ul>
      )}
      <p>{text('scr01_hierarchy.existing_deadlines_unchanged')}</p>
    </>
  );
}

export function LoopConfiguration({ plantId }: { readonly plantId: string }) {
  const context = useHierarchyScope();
  const operation = 'GET /loop/plants/{plant_id}/configuration';
  const query = useScreenQuery({
    key: screenKey({ screen: 'scr01', operation, ...context, params: { plant_id: plantId } }),
    policy: policyFor(operation),
    read: ({ signal }) => hierarchyPort.configuration.read({ path: { plant_id: plantId } }, signal),
  });
  return (
    <section aria-label={text('scr01_hierarchy.loop_configuration')}>
      <h3>{text('scr01_hierarchy.loop_configuration')}</h3>
      <HierarchyRefresh query={query} operation={operation} />
      <HierarchyResult query={query}>
        {query.data !== undefined && <LoopConfigurationView configuration={query.data} />}
        {query.data !== undefined && (
          <LoopConfigurationForm initial={query.data} plantId={plantId} />
        )}
      </HierarchyResult>
    </section>
  );
}
