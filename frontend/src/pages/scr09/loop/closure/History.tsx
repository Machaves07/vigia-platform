/** RV-13: selección explícita del historial, conservado aunque el lazo se reabra (P4). */
import { AttachmentReadLink } from '@/components/DirectUploadField/AttachmentReadLink';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  closureClock,
  closurePort,
  RECORD_TYPES,
  recordContent,
  stringField,
  type LedgerRecord,
} from '@/ports/scr09Closure';
import { text } from '@/texts';
import { label } from '@/texts/labels';

const TEXT_FIELDS = [
  'description',
  'performed_by_role',
  'verification_note',
  'closure_description',
  'evaluated_alternative',
  'rejection_reason',
  'responsible_display_name',
  'decision_date',
  'compensatory_measure',
  'review_note',
  'reason_text',
] as const;
const INSTANT_FIELDS = [
  'performed_at',
  'signed_at',
  'declared_at',
  'review_due_at',
  'new_review_due_at',
  'reviewed_at',
  'reopened_at',
] as const;
export function ClosureHistory({
  records,
  timeZone,
  canReadAttachments = false,
  organizationId,
  concessionOrganizationId = null,
  reportContractError,
}: {
  readonly records: readonly LedgerRecord[];
  readonly timeZone: string;
  readonly canReadAttachments?: boolean;
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { readonly code: string }) => void;
}) {
  return (
    <section aria-label={text('scr09.closure.history')}>
      <h2>{text('scr09.closure.history')}</h2>
      {records.length === 0 && <p>{text('scr09.closure.empty')}</p>}
      <ol>
        {records.map((record) => {
          if (
            record.organization_id !== organizationId &&
            record.organization_id !== concessionOrganizationId
          ) {
            reportContractError?.({ code: 'foreign_organization' });
            return null;
          }
          if (!RECORD_TYPES.includes(record.record_type)) return null;
          const content = recordContent(record);
          const signer = content.signer ?? content.reviewer;
          const signature =
            signer !== null && typeof signer === 'object' && !Array.isArray(signer)
              ? (signer as Record<string, unknown>)
              : {};
          const attachments = Array.isArray(content.attachments) ? content.attachments : [];
          return (
            <li key={record.record_id} data-record-type={record.record_type}>
              <h3>{text(`scr09.closure.record.${record.record_type}`)}</h3>
              {record.record_type === 'unclosable_state_declared' && (
                <p>{label('finding_lifecycle_state', 'unclosable')}</p>
              )}
              <TimestampWithOffset instant={record.received_at} timeZone={timeZone} />
              {TEXT_FIELDS.map((field) =>
                typeof content[field] === 'string' ? (
                  <p key={field}>{stringField(content[field])}</p>
                ) : null,
              )}
              {INSTANT_FIELDS.map((field) =>
                typeof content[field] === 'string' ? (
                  <p key={field}>
                    <TimestampWithOffset
                      instant={stringField(content[field])}
                      timeZone={timeZone}
                    />
                  </p>
                ) : null,
              )}
              {typeof signature.display_name === 'string' && (
                <p>
                  {text('scr09.closure.signed_by')}: {signature.display_name}
                </p>
              )}
              {typeof signature.professional_license === 'string' && (
                <p>
                  {text('scr09.closure.license')}: {signature.professional_license}
                </p>
              )}
              {typeof content.outcome === 'string' && (
                <p>{label('unclosable_review_outcome', content.outcome)}</p>
              )}
              {typeof content.escalation_level === 'string' && (
                <p>
                  {text('scr09.overdue.level')}:{' '}
                  {label('escalation_level', content.escalation_level)}
                </p>
              )}
              {canReadAttachments &&
                attachments.map((value: unknown, index) => {
                  if (
                    value === null ||
                    typeof value !== 'object' ||
                    !('attachment_id' in value) ||
                    typeof value.attachment_id !== 'string'
                  )
                    return null;
                  return (
                    <AttachmentReadLink
                      key={index}
                      attachmentId={value.attachment_id}
                      clock={closureClock()}
                      requestUrl={async (attachmentId) =>
                        closurePort.readAttachment.write({ path: { attachment_id: attachmentId } })
                      }
                    />
                  );
                })}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
