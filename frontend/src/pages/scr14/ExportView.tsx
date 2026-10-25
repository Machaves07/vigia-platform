/** RV-18: proyección explícita; ningún objeto del servidor se propaga al DOM. */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { ScreenState } from '@/pages/shared/ScreenState';
import type { Checkpoints, ExportReadView } from '@/ports/scr14Exports';
import { text } from '@/texts';
import { label } from '@/texts/labels';

export function ExportView({
  job,
  organizationId,
  timeZone,
}: {
  readonly job: ExportReadView;
  readonly organizationId: string;
  readonly timeZone: string;
}) {
  return (
    <section aria-label={text('scr14.manifest')}>
      <h3>{text('scr14.manifest')}</h3>
      <dl>
        <dt>{text('scr14.organization')}</dt>
        <dd>{organizationId}</dd>
        <dt>{text('scr14.plant')}</dt>
        <dd>{job.plant_id}</dd>
        <dt>{text('scr14.zone_ids')}</dt>
        <dd>{job.zone_ids.join(', ')}</dd>
        <dt>{text('scr14.period')}</dt>
        <dd>
          <TimestampWithOffset instant={job.period.from} timeZone={timeZone} /> —{' '}
          <TimestampWithOffset instant={job.period.to} timeZone={timeZone} />
        </dd>
        <dt>{text('scr14.package_form')}</dt>
        <dd>{label('package_form', job.package_form)}</dd>
        <dt>{text('scr14.status')}</dt>
        <dd>{label('export_state', job.export_state)}</dd>
        {job.manifest_summary !== null && (
          <>
            <dt>{text('scr14.archive_hash')}</dt>
            <dd>
              <code>{job.manifest_summary.archive_sha256}</code>
            </dd>
            <dt>{text('scr14.document_hash')}</dt>
            <dd>
              <code>{job.manifest_summary.portable_sha256}</code>
            </dd>
          </>
        )}
      </dl>
      <p>{text('scr14.no_removal')}</p>
    </section>
  );
}

export function CheckpointsView({
  data,
  organizationId,
  concessionOrganizationId,
  reportContractError,
}: {
  readonly data: Checkpoints;
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { code: string }) => void;
}) {
  if (
    data.organization_id !== organizationId &&
    data.organization_id !== concessionOrganizationId
  ) {
    reportContractError?.({ code: 'foreign_organization' });
    return <ScreenState state="error" />;
  }
  if (data.checkpoints.length === 0)
    return <ScreenState state="empty" message={text('scr14.no_checkpoints')} />;
  return (
    <table>
      <caption>{text('scr14.checkpoints')}</caption>
      <thead>
        <tr>
          <th scope="col">{text('scr14.plant')}</th>
          <th scope="col">{text('scr14.sequence')}</th>
          <th scope="col">{text('scr14.checkpoint_hash')}</th>
        </tr>
      </thead>
      <tbody>
        {data.checkpoints.map((point, index) => (
          <tr key={index}>
            <td>{point.chain.plant_id ?? text('scr14.organization')}</td>
            <td>{point.sequence}</td>
            <td>
              <code>{point.entry_hash}</code>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
