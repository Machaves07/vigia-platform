/** SCR-01: los límites de concesión pertenecen al esquema; la relación entre ellos, al servidor. */
import { useId, useState } from 'react';
import {
  CONCESSION_FIELDS,
  concessionLimits,
  hierarchyPort,
  settingsIssues,
  type OrganizationSettings,
  type SettingsBody,
} from '@/ports/scr01Hierarchy';
import type { ValidationIssue } from '@/ports/validation';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { text } from '@/texts';
import { AdminField, AdminMutationResult } from '../hierarchy/FormFields';
import {
  HierarchyNav,
  HierarchyRefresh,
  HierarchyResult,
  useHierarchyScope,
} from '../hierarchy/shared';
import styles from '../scr01.module.css';

function SettingsForm({ initial }: { readonly initial: OrganizationSettings }) {
  const noticeId = useId();
  const [values, setValues] = useState({
    concession_max_days: String(initial.concession_max_days),
    concession_default_days: String(initial.concession_default_days),
  });
  const [issues, setIssues] = useState<readonly ValidationIssue[]>([]);
  const mutation = useScreenMutation({
    write: (body: SettingsBody) => hierarchyPort.saveSettings.write({ body }),
    invalidates: { screen: 'scr01', resources: ['organization_settings'] },
  });
  return (
    <form
      className={styles.form}
      aria-label={text('scr01_hierarchy.concession_settings')}
      aria-describedby={mutation.error === null ? undefined : noticeId}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        // Vacío no se convierte a cero: el validador generado lo rechaza como número no finito.
        const body: SettingsBody = {
          concession_max_days:
            values.concession_max_days === '' ? NaN : Number(values.concession_max_days),
          concession_default_days:
            values.concession_default_days === '' ? NaN : Number(values.concession_default_days),
        };
        const found = settingsIssues(body);
        setIssues(found);
        if (found.length > 0 || mutation.status === 'pending') return;
        void mutation.run(body).catch(() => undefined);
      }}
    >
      {CONCESSION_FIELDS.map((field) => (
        <AdminField
          key={field}
          name={field}
          value={values[field]}
          type="number"
          min={concessionLimits[field].min}
          max={concessionLimits[field].max}
          issues={issues}
          disabled={mutation.status === 'pending'}
          onChange={(value) => {
            setValues({ ...values, [field]: value });
          }}
        />
      ))}
      <button className={styles.button} type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_hierarchy.save')}
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

export default function Page() {
  const context = useHierarchyScope();
  const operation = 'GET /organization/settings';
  const query = useScreenQuery({
    key: screenKey({ screen: 'scr01', operation, ...context }),
    policy: policyFor(operation),
    read: ({ signal }) => hierarchyPort.settings.read({}, signal),
  });
  return (
    <main className={styles.page}>
      <HierarchyNav />
      <h1>{text('scr01_hierarchy.organization')}</h1>
      <HierarchyRefresh query={query} operation={operation} />
      <HierarchyResult query={query}>
        {query.data !== undefined && <SettingsForm initial={query.data} />}
      </HierarchyResult>
    </main>
  );
}
