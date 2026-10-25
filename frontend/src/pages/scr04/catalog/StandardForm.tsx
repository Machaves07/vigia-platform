/** Declaración y sustitución append-only: el predicado viaja sin transformaciones. */
import { useId, useState } from 'react';
import { StepWizard } from '@/components/StepWizard/StepWizard';
import { ReasonField, reasonIssueKey } from '@/components/ReasonField/ReasonField';
import { FieldNotice, NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { noticeForField } from '@/components/NoticeStack/notice';
import {
  admittedFamilies,
  checkFreeText,
  CATALOG_REASON_LIMITS as LIMITS,
  catalogClock,
  catalogNotice,
  catalogPort,
  parsePredicate,
  predicateTemplate,
  type Standard,
  type StandardBody,
} from '@/ports/scr04Catalog';
import { useScreenMutation } from '@/state/query';
import { text } from '@/texts';
import { label } from '@/texts/labels';
import { Refresh, Result, useCatalogQuery } from './shared';

export function StandardForm({
  zoneId,
  plantId,
  standard,
}: {
  readonly zoneId: string;
  readonly plantId: string;
  readonly standard?: Standard;
}) {
  const id = useId();
  const [family, setFamily] = useState<StandardBody['family'] | ''>(standard?.family ?? '');
  const [title, setTitle] = useState(standard?.title_es ?? '');
  const [declared, setDeclared] = useState(standard?.declared_text ?? '');
  const [predicate, setPredicate] = useState(
    standard === undefined ? '' : JSON.stringify(standard.predicate),
  );
  const [reason, setReason] = useState('');
  const families = useCatalogQuery('GET /plants/{plant_id}/admissions', plantId, (signal) =>
    admittedFamilies(plantId, signal),
  );
  const mutation = useScreenMutation({
    write: async (body: StandardBody) =>
      standard === undefined
        ? catalogPort.declare.write({ path: { zone_id: zoneId }, body })
        : catalogPort.newVersion.write({
            path: { zone_id: zoneId, standard_id: standard.standard_id },
            body: {
              changes: {
                title_es: body.title_es,
                declared_text: body.declared_text,
                predicate: body.predicate,
              },
              reason_es: body.reason_es,
            },
          }),
    invalidates: { screen: 'scr04', resources: ['catalog', 'regression'] },
  });
  const parsed = parsePredicate(family, predicate);
  const titleCheck = checkFreeText(title, { minLength: 1, maxLength: 120 });
  const declaredCheck = checkFreeText(declared, { minLength: 1, maxLength: 4000 });
  const notice = catalogNotice(mutation.error);
  const notices = notice === null ? [] : [notice];
  const fieldNotice = (name: string) => noticeForField(notices, name);
  const fieldError = (name: string) => {
    const value = fieldNotice(name);
    return value === undefined ? null : <FieldNotice id={`${id}-${name}-notice`} notice={value} />;
  };
  const predicateInvalid = predicate !== '' && parsed === null;
  const summary = (
    <section>
      <h3>{text('scr04.catalog.summary')}</h3>
      <p>{title}</p>
      <p>{declared}</p>
      <p>{reason}</p>
    </section>
  );
  return (
    <>
      <NoticeStack
        notices={notices}
        clock={catalogClock()}
        onDismiss={() => {
          mutation.reset();
        }}
      />
      {fieldError('form')}
      {mutation.error !== null && notice === null ? (
        <p role="alert">{text('validation.invalid')}</p>
      ) : null}
      {mutation.status === 'success' ? <p role="status">{text('scr04.catalog.saved')}</p> : null}
      <StepWizard
        title={text(standard === undefined ? 'scr04.catalog.declare' : 'scr04.catalog.new_version')}
        dirty={
          mutation.status !== 'success' &&
          (title !== (standard?.title_es ?? '') ||
            declared !== (standard?.declared_text ?? '') ||
            predicate !== (standard === undefined ? '' : JSON.stringify(standard.predicate)) ||
            reason !== '' ||
            family !== (standard?.family ?? ''))
        }
        steps={[
          {
            id: 'declaration',
            title: text('scr04.catalog.declare'),
            complete: family !== '' && titleCheck.ok && declaredCheck.ok,
            content: (
              <>
                <Refresh query={families} />
                <Result query={families}>
                  <label htmlFor={`${id}-family`}>{text('scr04.catalog.family')}</label>
                  <select
                    id={`${id}-family`}
                    value={family}
                    disabled={standard !== undefined}
                    aria-invalid={fieldNotice('family') !== undefined}
                    aria-describedby={
                      fieldNotice('family') === undefined ? undefined : `${id}-family-notice`
                    }
                    onChange={(event) => {
                      setFamily(event.target.value as StandardBody['family']);
                    }}
                  >
                    <option value="">{text('scr04.catalog.select')}</option>
                    {[
                      ...new Set([
                        ...(families.data ?? []),
                        ...(standard === undefined ? [] : [standard.family]),
                      ]),
                    ].map((value) => (
                      <option key={value} value={value}>
                        {label('predicate_family', value)}
                      </option>
                    ))}
                  </select>
                  {fieldError('family')}
                </Result>
                <label htmlFor={`${id}-title`}>{text('scr04.catalog.title_es')}</label>
                <input
                  id={`${id}-title`}
                  value={title}
                  aria-invalid={title !== '' && !titleCheck.ok}
                  onChange={(event) => {
                    setTitle(event.target.value);
                  }}
                />
                {title !== '' && !titleCheck.ok ? <p role="alert">{text(titleCheck.key)}</p> : null}
                <label htmlFor={`${id}-declared`}>{text('scr04.catalog.declared_text')}</label>
                <textarea
                  id={`${id}-declared`}
                  value={declared}
                  aria-invalid={!declaredCheck.ok || fieldNotice('declared_text') !== undefined}
                  aria-describedby={
                    fieldNotice('declared_text') === undefined
                      ? undefined
                      : `${id}-declared_text-notice`
                  }
                  onChange={(event) => {
                    setDeclared(event.target.value);
                  }}
                />
                {declared !== '' && !declaredCheck.ok ? (
                  <p role="alert">{text(declaredCheck.key)}</p>
                ) : null}
                {fieldError('declared_text')}
              </>
            ),
          },
          {
            id: 'predicate',
            title: text('scr04.catalog.predicate'),
            complete: parsed !== null,
            content: (
              <>
                <label htmlFor={`${id}-predicate`}>{text('scr04.catalog.predicate')}</label>
                <p id={`${id}-help`}>{text('scr04.catalog.predicate_help')}</p>
                <button
                  type="button"
                  onClick={() => {
                    if (family !== '') setPredicate(predicateTemplate(family));
                  }}
                >
                  {text('scr04.catalog.use_template')}
                </button>
                <textarea
                  id={`${id}-predicate`}
                  value={predicate}
                  aria-invalid={predicateInvalid || fieldNotice('predicate') !== undefined}
                  aria-describedby={`${id}-help${predicateInvalid ? ` ${id}-error` : ''}${fieldNotice('predicate') === undefined ? '' : ` ${id}-predicate-notice`}`}
                  onChange={(event) => {
                    setPredicate(event.target.value);
                  }}
                />
                {predicateInvalid ? (
                  <p id={`${id}-error`} role="alert">
                    {text('scr04.catalog.predicate_invalid')}
                  </p>
                ) : null}
                {fieldError('predicate')}
              </>
            ),
          },
          {
            id: 'reason',
            title: text('scr04.catalog.reason'),
            complete: reasonIssueKey(reason, LIMITS) === null,
            content: (
              <ReasonField
                label={text('scr04.catalog.reason')}
                value={reason}
                onChange={setReason}
                limits={LIMITS}
                notice={fieldNotice('reason_es')}
              />
            ),
          },
        ]}
        summary={summary}
        onSubmit={async () => {
          if (
            family === '' ||
            parsed === null ||
            !titleCheck.ok ||
            !declaredCheck.ok ||
            reasonIssueKey(reason, LIMITS) !== null
          )
            throw new Error('Declaración incompleta.');
          await mutation.run({
            family,
            title_es: titleCheck.normalized,
            declared_text: declaredCheck.normalized,
            predicate: parsed,
            reason_es: reason.normalize('NFC'),
          });
        }}
      />
      {notice !== null ? (
        <aside aria-label={text('scr04.catalog.summary')}>
          {notices
            .filter((value) => value.field !== null && value.field !== 'form')
            .map((value) => (
              <FieldNotice id={`${id}-summary-notice`} key={value.id} notice={value} />
            ))}
        </aside>
      ) : null}
    </>
  );
}
