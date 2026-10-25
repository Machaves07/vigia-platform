import { useId, useState } from 'react';
import { hierarchyPort, zoneIssues, type ZoneBody } from '@/ports/scr01Hierarchy';
import type { ValidationIssue } from '@/ports/validation';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { AdminField, AdminMutationResult } from './FormFields';
import styles from '../scr01.module.css';

export function ZoneForm({ plantId }: { readonly plantId: string }) {
  const noticeId = useId();
  const [body, setBody] = useState<ZoneBody>({ code: '', name: '' });
  const [issues, setIssues] = useState<readonly ValidationIssue[]>([]);
  const mutation = useScreenMutation({
    write: (input: ZoneBody) =>
      hierarchyPort.createZone.write({ path: { plant_id: plantId }, body: input }),
    invalidates: { screen: 'scr01', resources: ['hierarchy'] },
  });
  return (
    <form
      className={styles.form}
      aria-label={text('scr01_hierarchy.create_zone')}
      aria-describedby={mutation.error === null ? undefined : noticeId}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        const found = zoneIssues(body);
        setIssues(found);
        if (found.length > 0 || mutation.status === 'pending') return;
        void mutation.run(body).then(
          () => {
            setBody({ code: '', name: '' });
          },
          () => undefined,
        );
      }}
    >
      <h3>{text('scr01_hierarchy.create_zone')}</h3>
      {(['code', 'name'] as const).map((field) => (
        <AdminField
          key={field}
          name={field}
          value={body[field]}
          issues={issues}
          disabled={mutation.status === 'pending'}
          onChange={(value) => {
            setBody({ ...body, [field]: value });
          }}
        />
      ))}
      <button className={styles.button} type="submit" disabled={mutation.status === 'pending'}>
        {text('scr01_hierarchy.create_zone')}
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
