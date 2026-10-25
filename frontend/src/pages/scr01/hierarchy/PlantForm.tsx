/** Alta sin selector de organización o región: la región procede del despliegue. */
import { useId, useState } from 'react';
import {
  deploymentDataRegion,
  hierarchyPort,
  plantIssues,
  type PlantBody,
} from '@/ports/scr01Hierarchy';
import type { ValidationIssue } from '@/ports/validation';
import { useScreenMutation } from '@/state/query';
import { ScreenState } from '@/pages/shared/ScreenState';
import { text } from '@/texts';
import { AdminField, AdminMutationResult } from './FormFields';
import styles from '../scr01.module.css';

const FIELDS = ['code', 'name', 'country', 'timezone'] as const;

export function PlantForm() {
  const noticeId = useId();
  const region = deploymentDataRegion();
  const [values, setValues] = useState({ code: '', name: '', country: '', timezone: '' });
  const [issues, setIssues] = useState<readonly ValidationIssue[]>([]);
  const mutation = useScreenMutation({
    write: (body: PlantBody) => hierarchyPort.createPlant.write({ body }),
    invalidates: { screen: 'scr01', resources: ['hierarchy'] },
  });
  return (
    <form
      className={styles.form}
      aria-label={text('scr01_hierarchy.create_plant')}
      noValidate
      aria-describedby={mutation.error === null ? undefined : noticeId}
      onSubmit={(event) => {
        event.preventDefault();
        if (region === null || mutation.status === 'pending') return;
        const body: PlantBody = { ...values, data_region: region };
        const found = plantIssues(body);
        setIssues(found);
        if (found.length > 0) return;
        void mutation.run(body).then(
          () => {
            setValues({ code: '', name: '', country: '', timezone: '' });
          },
          () => undefined,
        );
      }}
    >
      <h2>{text('scr01_hierarchy.create_plant')}</h2>
      <p>
        {text('scr01_hierarchy.data_region')}:{' '}
        <span data-part="plant-region">
          {region ?? text('scr01_hierarchy.region_unconfigured')}
        </span>
      </p>
      {region === null && (
        <>
          <ScreenState state="unavailable" />
          <p>{text('scr01_hierarchy.region_unconfigured')}</p>
        </>
      )}
      {FIELDS.map((field) => (
        <AdminField
          key={field}
          name={field}
          value={values[field]}
          issues={issues}
          disabled={mutation.status === 'pending'}
          onChange={(value) => {
            setValues({ ...values, [field]: value });
          }}
        />
      ))}
      <button
        className={styles.button}
        type="submit"
        disabled={region === null || mutation.status === 'pending'}
      >
        {text('scr01_hierarchy.create_plant')}
      </button>
      <AdminMutationResult
        id={noticeId}
        error={mutation.error}
        status={mutation.status}
        onDismiss={mutation.reset}
      />
    </form>
  );
}
