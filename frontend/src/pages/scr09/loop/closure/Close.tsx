/** La firma es explícita; ni la falta de licencia ni el estado ocultan la acción. */
import { useState } from 'react';
import closeBody from '../../../../../openapi/bodies/close_loop_findings__finding_state_id__closure_post.json';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { reasonLimits } from '@/components/ReasonField/limits';
import {
  optionalText,
  ReasonField,
  reasonIssueKey,
  submittedText,
} from '@/components/ReasonField/ReasonField';
import { textFields } from '@/components/ReasonField/TextNotice';
import { SignatureBlock, type SignerProfile } from '@/components/SignatureBlock/SignatureBlock';
import { closureNotice, closurePort, type ClosureBody } from '@/ports/scr09Closure';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
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
  const [limits] = useState(() => ({
    description: reasonLimits(closeBody, 'closure_description'),
    verification: reasonLimits(closeBody, 'verification_note'),
  }));
  const mutation = useScreenMutation({
    write: (body: ClosureBody) =>
      closurePort.close.write({ path: { finding_state_id: context.findingId }, body }),
    invalidates: { screen: 'scr09', resources: ['findings', 'actions', 'ledger_records'] },
  });
  const notice = closureNotice(mutation.error, {
    loop_signer_license_missing: 'signature',
    loop_closure_evidence_missing: 'evidence',
    loop_closure_requires_assigned_action: 'action',
    ...textFields({ closure_description: description, verification_note: verification }),
  });
  const textNotice = (field: string) => (notice?.field === field ? notice : undefined);
  const sign = (snapshot: SignerProfile) => {
    if (
      reasonIssueKey(description, limits.description, { loopText: true }) !== null ||
      reasonIssueKey(verification, limits.verification, { loopText: true, optional: true }) !== null
    ) {
      setInvalid(true);
      return;
    }
    void mutation
      .run({
        closure_description: submittedText(description),
        verification_note: optionalText(verification),
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
        limits={limits.description}
        loopText
        showErrors={invalid}
        notice={textNotice('closure_description')}
      />
      <ReasonField
        name="verification_note"
        label={text('scr09.closure.verification')}
        value={verification}
        onChange={setVerification}
        limits={limits.verification}
        optional
        loopText
        showErrors={invalid}
        notice={textNotice('verification_note')}
      />
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
      ) : notice?.field === 'signature' ||
        notice?.field === 'closure_description' ||
        notice?.field === 'verification_note' ? null : (
        <Failure error={mutation.error} written={[description, verification].join('\n')} />
      )}
      {context.principal !== null && (
        <SignatureBlock
          principal={{
            display_name: context.principal.display_name,
            professional_license: context.principal.professional_license ?? null,
          }}
          textToSign={`${text('scr09.closure.sign_text')}\n${submittedText(description)}\n${submittedText(verification)}`}
          notice={notice?.field === 'signature' ? notice : undefined}
          pending={mutation.status === 'pending'}
          onSign={sign}
        />
      )}
      {mutation.status === 'success' && <p role="status">{text('scr09.closure.saved')}</p>}
    </section>
  );
}
