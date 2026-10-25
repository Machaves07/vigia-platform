/** La firma es explícita; ni la falta de licencia ni el estado ocultan la acción. */
import { useState } from 'react';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { SignatureBlock, type SignerProfile } from '@/components/SignatureBlock/SignatureBlock';
import { closureNotice, closurePort, type ClosureBody } from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { LONG_TEXT } from './Evidence';
import { Failure, type ClosureContext } from './common';

export function Close({
  context,
  onWritten,
}: {
  readonly context: ClosureContext;
  readonly onWritten: () => void;
}) {
  const [description, setDescription] = useState('');
  const [verification, setVerification] = useState('');
  const [invalid, setInvalid] = useState(false);
  const mutation = useScreenMutation({
    write: (body: ClosureBody) =>
      closurePort.close.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: { screen: 'scr09', resources: ['findings', 'actions', 'ledger_records'] },
  });
  const notice = closureNotice(mutation.error, {
    loop_signer_license_missing: 'signature',
    loop_closure_evidence_missing: 'evidence',
    loop_closure_requires_assigned_action: 'action',
  });
  const sign = (snapshot: SignerProfile) => {
    if (reasonIssueKey(description, LONG_TEXT) !== null) {
      setInvalid(true);
      return;
    }
    void mutation
      .run({
        closure_description: description,
        verification_note: verification.trim() === '' ? null : verification,
        professional_license: snapshot.professional_license,
      })
      .then(onWritten)
      .catch(() => undefined);
  };
  return (
    <section aria-label={text('scr09.closure.sign')}>
      <h2>{text('scr09.closure.sign')}</h2>
      <ReasonField
        name="closure_description"
        label={text('scr09.closure.sign_description')}
        value={description}
        onChange={setDescription}
        limits={LONG_TEXT}
        showErrors={invalid}
      />
      <label>
        {text('scr09.closure.verification')}
        <textarea
          maxLength={1000}
          value={verification}
          onChange={(e) => {
            setVerification(e.target.value);
          }}
        />
      </label>
      {notice?.field === 'action' || notice?.field === 'evidence' ? (
        <div data-anchor={notice.field}>
          <FieldNotice id={notice.id} notice={notice} />
          <p>
            {text(
              notice.field === 'action'
                ? 'scr09.closure.action_guide'
                : 'scr09.closure.evidence_guide',
            )}
          </p>
        </div>
      ) : notice?.field === 'signature' ? null : (
        <Failure error={mutation.error} />
      )}
      {context.principal !== null && (
        <SignatureBlock
          principal={{
            display_name: context.principal.display_name,
            professional_license: context.principal.professional_license ?? null,
          }}
          textToSign={`${text('scr09.closure.sign_text')}\n${description}\n${verification}`}
          notice={notice?.field === 'signature' ? notice : undefined}
          pending={mutation.status === 'pending'}
          onSign={sign}
        />
      )}
      {mutation.status === 'success' && <p role="status">{text('scr09.closure.saved')}</p>}
    </section>
  );
}
