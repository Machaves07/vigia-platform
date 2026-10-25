import { useState } from 'react';
import { fleetFailure, fleetPort, type FleetThresholds } from '@/ports/scr07Fleet';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { FleetNav, FleetRefresh, FleetResult, useFleetContext } from '../shared';
import styles from './inventory.module.css';

function ThresholdForm({
  plantId,
  initial,
}: {
  readonly plantId: string;
  readonly initial: FleetThresholds;
}) {
  const mutation = useScreenMutation({
    write: (body: FleetThresholds) =>
      fleetPort.saveThresholds.write({ path: { plant_id: plantId }, body }),
    invalidates: { screen: 'scr07', resources: ['fleet_thresholds', 'nodes', 'fleet_alarms'] },
  });
  const [values, setValues] = useState({
    queue_pending_threshold: String(initial.queue_pending_threshold),
    queue_age_threshold_minutes: String(initial.queue_age_threshold_minutes),
    clock_drift_threshold_ms: String(initial.clock_drift_threshold_ms),
  });
  const fields = [
    'queue_pending_threshold',
    'queue_age_threshold_minutes',
    'clock_drift_threshold_ms',
  ] as const;
  const labels = ['scr07.queue_threshold', 'scr07.age_threshold', 'scr07.drift_threshold'];
  return (
    <form
      className={styles.fields}
      onSubmit={(event) => {
        event.preventDefault();
        void mutation
          .run({
            queue_pending_threshold: Number(values.queue_pending_threshold),
            queue_age_threshold_minutes: Number(values.queue_age_threshold_minutes),
            clock_drift_threshold_ms: Number(values.clock_drift_threshold_ms),
          })
          .catch(() => undefined);
      }}
    >
      {fields.map((field, index) => (
        <label key={field}>
          {text(labels[index] ?? 'scr07.unknown')}
          <input
            name={field}
            type="number"
            required
            min={1}
            max={2147483647}
            step={1}
            disabled={mutation.status === 'pending'}
            value={values[field]}
            onChange={(event) => {
              mutation.reset();
              setValues({ ...values, [field]: event.currentTarget.value });
            }}
          />
        </label>
      ))}
      <button type="submit" disabled={mutation.status === 'pending'}>
        {text('scr07.save')}
      </button>
      {mutation.status === 'success' && <p role="status">{text('scr07.saved')}</p>}
      {mutation.status === 'error' && (
        <p role="alert">
          {text(
            mutation.error !== null && fleetFailure(mutation.error).state === 'unavailable'
              ? 'screen_state.unavailable'
              : 'screen_state.error',
          )}
        </p>
      )}
    </form>
  );
}

export function Thresholds({ plantId }: { readonly plantId: string }) {
  const context = useFleetContext();
  const operation = 'GET /plants/{plant_id}/fleet-thresholds';
  const query = useScreenQuery({
    key: screenKey({
      screen: 'scr07',
      operation,
      scope: `${context.scope}:${plantId}`,
      concession: context.concession,
      params: { plant_id: plantId },
    }),
    policy: policyFor(operation),
    read: ({ signal }) => fleetPort.thresholds.read({ path: { plant_id: plantId } }, signal),
  });
  return (
    <main>
      <FleetNav />
      <h1>{text('scr07.thresholds')}</h1>
      <FleetRefresh query={query} operation={operation} />
      <FleetResult query={query} empty={false}>
        {query.data !== undefined && <ThresholdForm plantId={plantId} initial={query.data} />}
      </FleetResult>
    </main>
  );
}
