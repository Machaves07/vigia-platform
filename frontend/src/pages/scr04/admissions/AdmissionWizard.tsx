/** El no completa un paso igual que el sí; solo el servidor decide la admisión. */
import { useState } from 'react';
import { StepWizard } from '@/components/StepWizard/StepWizard';
import { WriteReceipt } from '@/components/WriteReceipt/WriteReceipt';
import {
  admissionRejection,
  isRecordedRejection,
  CRITERIA,
  FAMILIES,
  parametersPort,
  screenLabel,
  type Admission,
  type AdmissionBody,
  type Criterion,
} from '@/ports/scr04Parameters';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { Failure } from '../parameters/shared';

export function AdmissionWizard({
  plantId,
  onRecorded,
}: {
  readonly plantId: string;
  readonly onRecorded: () => void;
}) {
  const [family, setFamily] = useState<AdmissionBody['family']>('coexistence');
  const [answers, setAnswers] = useState<Partial<Record<Criterion, boolean>>>({});
  const [result, setResult] = useState<{
    criterion: Criterion | null;
    admission: Admission | null;
    rejected: boolean;
  } | null>(null);
  const [run, setRun] = useState(0);
  const mutation = useScreenMutation({
    write: (body: AdmissionBody) =>
      parametersPort.evaluate.write({ path: { plant_id: plantId }, body }),
    invalidates: { screen: 'scr04', resources: ['admissions'] },
  });
  const submit = async () => {
    const body: AdmissionBody = {
      family,
      answers: {
        standard: answers.standard === true,
        remedy: answers.remedy === true,
        subject: answers.subject === true,
      },
    };
    try {
      const admission = await mutation.run(body);
      setResult({
        criterion: admission.failed_criterion,
        admission,
        rejected: admission.result === 'rejected',
      });
    } catch (error) {
      if (!isRecordedRejection(error)) throw error;
      const criterion = await admissionRejection(error, plantId, body);
      setResult({ criterion, admission: null, rejected: true });
    }
    onRecorded();
  };
  return (
    <section>
      {result === null ? (
        <>
          <label>
            {text('scr04.admissions.family')}
            <select
              value={family}
              onChange={(event) => {
                setFamily(event.target.value as AdmissionBody['family']);
              }}
            >
              {FAMILIES.map((value) => (
                <option key={value} value={value}>
                  {screenLabel('predicate_family', value)}
                </option>
              ))}
            </select>
          </label>
          <StepWizard
            key={run}
            title={text('scr04.admissions.title')}
            dirty={Object.keys(answers).length > 0}
            steps={CRITERIA.map((criterion) => ({
              id: criterion,
              title: text(`scr04.admissions.${criterion}`),
              complete: typeof answers[criterion] === 'boolean',
              content: (
                <fieldset>
                  <legend>{text(`scr04.admissions.${criterion}`)}</legend>
                  <p>{text(`scr04.admissions.${criterion}_help`)}</p>
                  {([true, false] as const).map((value) => (
                    <label key={String(value)}>
                      <input
                        type="radio"
                        name={criterion}
                        checked={answers[criterion] === value}
                        onChange={() => {
                          setAnswers({ ...answers, [criterion]: value });
                        }}
                      />
                      {text(value ? 'scr04.admissions.yes' : 'scr04.admissions.no')}
                    </label>
                  ))}
                </fieldset>
              ),
            }))}
            summary={
              <dl>
                {CRITERIA.map((criterion) => (
                  <div key={criterion}>
                    <dt>{text(`scr04.admissions.${criterion}`)}</dt>
                    <dd>
                      {text(
                        answers[criterion] === true
                          ? 'scr04.admissions.yes'
                          : 'scr04.admissions.no',
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            }
            onSubmit={submit}
          />
          <Failure error={mutation.error} />
        </>
      ) : (
        <>
          <h2>{text('scr04.admissions.recorded')}</h2>
          <p role="status" data-failed-criterion={result.criterion ?? ''}>
            {result.criterion === null
              ? result.rejected
                ? text('scr04.admissions.criterion_unavailable')
                : screenLabel('admission_result', 'admitted')
              : `${text('scr04.admissions.failed_criterion')}: ${screenLabel('admission_criterion', result.criterion)}`}
          </p>
          {result.admission !== null && (
            <WriteReceipt
              record={result.admission.ledger_record_id}
              roleInUse={result.admission.role_in_use}
            />
          )}
          <button
            type="button"
            onClick={() => {
              setResult(null);
              setAnswers({});
              mutation.reset();
              setRun(run + 1);
            }}
          >
            {text('scr04.admissions.new')}
          </button>
        </>
      )}
    </section>
  );
}
