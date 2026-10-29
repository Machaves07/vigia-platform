/** SCR-14 · IF-17: solicitar, consultar y descargar sin sondeos auditados. */
import { useState } from 'react';
import { useFormCheck } from '@/components/DateFields/useFormCheck';
import { ScreenState } from '@/pages/shared/ScreenState';
import { exportsClock, exportsPort, type ExportJob } from '@/ports/scr14Exports';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { Job } from '../Job';
import { RequestForm } from '../RequestForm';
import { Refresh, Result } from '../shared';
import styles from '../scr14.module.css';

function Exports() {
  const { principal } = useUiSession();
  const organizationId = principal?.organization.organization_id ?? '';
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({
      screen: 'scr14',
      operation: 'GET /hierarchy',
      scope: organizationId,
      concession,
    }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => exportsPort.hierarchy.read({}, signal),
  });
  const scope =
    concession === null ? organizationId : (hierarchy.data?.organization.organization_id ?? '');
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const [jobsAt, setJobsAt] = useState<number | null>(null);
  const [selected, setSelected] = useState<{ id: string; initial: ExportJob | null } | null>(null);
  const [exportId, setExportId] = useState('');
  const check = useFormCheck();
  const plants = hierarchy.data?.plants ?? [];
  const listedJobs = useScreenQuery({
    key: screenKey({
      screen: 'scr14',
      operation: 'GET /accreditation/exports/{export_id}',
      scope,
      concession,
      params: { export_ids: jobs.map((job) => job.export_id) },
    }),
    policy: policyFor('GET /accreditation/exports/{export_id}'),
    read: async ({ signal }) => {
      const results: ExportJob[] = [];
      for (const job of jobs)
        results.push(await exportsPort.status.read({ path: { export_id: job.export_id } }, signal));
      return results;
    },
  });
  return (
    <main className={styles.page}>
      <h1>{text('scr14.title')}</h1>
      <Refresh query={hierarchy} />
      <Result
        query={{
          ...hierarchy,
          error: hierarchy.data === undefined ? hierarchy.error : null,
          isFetching: hierarchy.data === undefined && hierarchy.isFetching,
        }}
      >
        <RequestForm
          plants={plants}
          scope={scope}
          concession={concession}
          onRequested={(job) => {
            setJobsAt(exportsClock().now());
            setJobs((old) => [job, ...old.filter((item) => item.export_id !== job.export_id)]);
            setSelected({ id: job.export_id, initial: job });
          }}
        />
      </Result>
      <section aria-label={text('scr14.jobs')}>
        <h2>{text('scr14.jobs')}</h2>
        {jobs.length > 0 && (
          <Refresh query={{ ...listedJobs, fetchedAt: listedJobs.fetchedAt ?? jobsAt }} />
        )}
        {jobs.length === 0 ? (
          <ScreenState state="empty" message={text('scr14.empty')} />
        ) : (
          <Result query={{ ...listedJobs, status: 'success' }}>
            <ul>
              {(listedJobs.data ?? jobs).map((job) => (
                <li key={job.export_id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelected({ id: job.export_id, initial: null });
                    }}
                  >
                    {job.export_id}
                  </button>{' '}
                  {label('export_state', job.export_state)}
                </li>
              ))}
            </ul>
          </Result>
        )}
      </section>
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (!check.run(event.currentTarget)) return;
          if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(exportId))
            setSelected({ id: exportId, initial: null });
        }}
      >
        <label>
          {text('scr14.export_id')}
          <input
            name="export_id"
            required
            value={exportId}
            {...check.field('export_id')}
            onChange={(event) => {
              setExportId(event.target.value);
              check.clear('export_id');
            }}
            pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
          />
          {check.error('export_id')}
        </label>
        <button type="submit">{text('scr14.open')}</button>
      </form>
      {selected !== null && scope !== '' && (
        <Job
          key={`${selected.id}:${selected.initial === null ? 'read' : 'new'}`}
          exportId={selected.id}
          initial={selected.initial}
          scope={scope}
          concession={concession}
          plants={plants}
        />
      )}
    </main>
  );
}

export default function Page() {
  const { principal } = useUiSession();
  return (
    <Exports
      key={`${principal?.organization.organization_id ?? ''}:${principal?.concession_id ?? ''}`}
    />
  );
}
