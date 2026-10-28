/**
 * Área de trabajo de un análisis iniciado (IF-10; BR-APP-34, 43, 45, 47, 49).
 *
 * - UI-15 con seis pasos: los cinco porqués (cada uno se guarda en el servidor con SUP-U05-17) y
 *   la causa raíz con su categoría cerrada, el enunciado y la conclusión (se envían al firmar).
 *   «Guardar sin terminar» existe porque hay `server_progress_ref` (el `analysis_id`) y guarda
 *   los porqués con cambios.
 * - El orden de los porqués, los textos y la licencia los valida el servidor: sus rechazos se
 *   anclan al campo (`loop_analysis_step_out_of_order`, `loop_blocked_vocabulary_term`,
 *   `loop_person_attribution_detected`, `loop_cause_category_not_allowed`,
 *   `loop_signer_license_missing`) y nada se deshabilita por una regla propia (BR-APP-43).
 * - UI-22 va al lado de la plantilla: su estado nunca la oculta ni la bloquea (PR-APP-14).
 * - UI-16 firma con SUP-U05-18; `loop_analysis_information_insufficient` deja el análisis
 *   «pendiente de información» con lo que falta y `409 loop_transition_not_allowed` (diferencia 9)
 *   se muestra anclado. «Enviar» del resumen y «Firmar» envían la misma y única firma.
 */
import { useId, useRef, useState } from 'react';

import {
  AssistantDraftPanel,
  ValidatorRejection,
} from '@/components/AssistantDraftPanel/AssistantDraftPanel';
import { rejectionCode, rejectionOf } from '@/components/AssistantDraftPanel/state';
import { useAssistantDraft } from '@/components/AssistantDraftPanel/useAssistantDraft';
import { FieldNotice, NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { noticeForField, type Notice } from '@/components/NoticeStack/notice';
import { SignatureBlock, type SignerProfile } from '@/components/SignatureBlock/SignatureBlock';
import { StepWizard } from '@/components/StepWizard/StepWizard';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  assistantFailure,
  noticeError,
  plantTimeZone,
  rootCauseClock,
  rootCausePort,
  type Analysis,
  type AnalysisSignatureBody,
  type AnalysisStepBody,
} from '@/ports/scr11RootCause';
import { policyFor, screenKey, useScreenMutation, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import styles from './root_cause.module.css';
import { PendingInformation, SavedWhys, SignedAnalysis } from './AnalysisViews';
import { ZoneContextPanel } from './ZoneContextPanel';

/** Las tres categorías cerradas de causa (proceso, ingeniería, organización). */
const CAUSE_CATEGORIES = ['process', 'engineering', 'organization'] as const;
const WHY_COUNT = 5;

interface WhyDraft {
  readonly statement: string;
  /** `''`: sin categoría. */
  readonly category: string;
}

interface RootDraft {
  readonly category: string;
  readonly statement: string;
  readonly conclusion: string;
}

const EMPTY_ROOT: RootDraft = { category: '', statement: '', conclusion: '' };

function savedStep(analysis: Analysis, index: number) {
  return analysis.why_steps.find((step) => step.step_index === index);
}

function draftsFrom(analysis: Analysis): WhyDraft[] {
  return Array.from({ length: WHY_COUNT }, (_, offset) => {
    const saved = savedStep(analysis, offset + 1);
    return { statement: saved?.statement ?? '', category: saved?.category ?? '' };
  });
}

/** El porqué en pantalla coincide con el guardado en el servidor. */
function matches(saved: ReturnType<typeof savedStep>, draft: WhyDraft | undefined): boolean {
  if (saved === undefined || draft === undefined) return false;
  return saved.statement === draft.statement && (saved.category ?? '') === draft.category;
}

function synced(analysis: Analysis, drafts: readonly WhyDraft[], offset: number): boolean {
  return matches(savedStep(analysis, offset + 1), drafts[offset]);
}

function blank(value: string): boolean {
  return value.trim() === '';
}

function notice(field: string, error: unknown): Notice {
  return { id: field, field, error: noticeError(error) };
}

/** Rechazo de los validadores de U-04 que el aviso trae, con término y sugerencia si vienen. */
function ValidatorDetail({ value }: { readonly value: Notice | undefined }) {
  if (value === undefined || rejectionCode(value.error.detail_code) === null) return null;
  return (
    <ValidatorRejection
      rejection={rejectionOf(value.error.body, value.error.detail_code ?? null)}
    />
  );
}

function WhyStep({
  analysis,
  offset,
  draft,
  onChange,
  onSave,
  saving,
  fieldNotice,
}: {
  readonly analysis: Analysis;
  readonly offset: number;
  readonly draft: WhyDraft;
  readonly onChange: (draft: WhyDraft) => void;
  readonly onSave: () => void;
  readonly saving: boolean;
  readonly fieldNotice: Notice | undefined;
}) {
  const id = useId();
  const index = offset + 1;
  const saved = savedStep(analysis, index);
  const question =
    saved?.question_es ?? (analysis.next_step_index === index ? analysis.next_question_es : null);
  const noticeId = `${id}-notice`;
  const isSynced = matches(saved, draft);
  return (
    <div className={styles.step} data-part={`why-${String(index)}`}>
      <p data-part="question">
        <strong>{text('scr11.question')}:</strong> {question ?? text('scr11.question_pending')}
      </p>
      {offset > 0 ? <p className={styles.hint}>{text('scr11.optional_step')}</p> : null}
      <label htmlFor={`${id}-statement`}>
        {text('scr11.statement')} {String(index)}
      </label>
      <textarea
        id={`${id}-statement`}
        name={`why-${String(index)}`}
        rows={4}
        value={draft.statement}
        aria-invalid={fieldNotice === undefined ? undefined : true}
        aria-describedby={fieldNotice === undefined ? undefined : noticeId}
        onChange={(event) => {
          onChange({ ...draft, statement: event.currentTarget.value });
        }}
      />
      <label htmlFor={`${id}-category`}>{text('scr11.category')}</label>
      <select
        id={`${id}-category`}
        value={draft.category}
        onChange={(event) => {
          onChange({ ...draft, category: event.currentTarget.value });
        }}
      >
        <option value="">{text('scr11.no_category')}</option>
        {CAUSE_CATEGORIES.map((category) => (
          <option key={category} value={category}>
            {label('cause_category', category)}
          </option>
        ))}
      </select>
      <p data-part="evidence">
        {text('scr11.evidence')}:{' '}
        {saved === undefined || saved.evidence_refs.length === 0
          ? text('scr11.no_evidence')
          : saved.evidence_refs.map((ref) => label('evidence_ref_kind', ref.kind)).join(', ')}
      </p>
      <button type="button" className={styles.button} disabled={saving} onClick={onSave}>
        {text(saving ? 'scr11.saving_step' : 'scr11.save_step')}
      </button>
      <p role="status" data-part="step-sync">
        {isSynced
          ? text('scr11.step_saved')
          : blank(draft.statement) && saved === undefined
            ? ''
            : text('scr11.step_unsaved')}
      </p>
      {fieldNotice === undefined ? null : (
        <>
          <FieldNotice id={noticeId} notice={fieldNotice} />
          <ValidatorDetail value={fieldNotice} />
        </>
      )}
    </div>
  );
}

function RootStep({
  root,
  onChange,
}: {
  readonly root: RootDraft;
  readonly onChange: (root: RootDraft) => void;
}) {
  const id = useId();
  return (
    <div className={styles.step} data-part="root">
      <p className={styles.hint}>{text('scr11.root_sent_on_sign')}</p>
      <label htmlFor={`${id}-category`}>{text('scr11.root_category')}</label>
      <select
        id={`${id}-category`}
        value={root.category}
        onChange={(event) => {
          onChange({ ...root, category: event.currentTarget.value });
        }}
      >
        <option value="">{text('scr11.choose_category')}</option>
        {CAUSE_CATEGORIES.map((category) => (
          <option key={category} value={category}>
            {label('cause_category', category)}
          </option>
        ))}
      </select>
      <label htmlFor={`${id}-statement`}>{text('scr11.root_statement')}</label>
      <textarea
        id={`${id}-statement`}
        rows={4}
        value={root.statement}
        onChange={(event) => {
          onChange({ ...root, statement: event.currentTarget.value });
        }}
      />
      <label htmlFor={`${id}-conclusion`}>{text('scr11.conclusion')}</label>
      <textarea
        id={`${id}-conclusion`}
        rows={4}
        value={root.conclusion}
        onChange={(event) => {
          onChange({ ...root, conclusion: event.currentTarget.value });
        }}
      />
    </div>
  );
}

/** Texto que se firma: los porqués guardados y la causa raíz que la persona escribió. */
function textToSign(analysis: Analysis, root: RootDraft): string {
  const whys = analysis.why_steps.map(
    (step) => `${text('scr11.why_title')} ${String(step.step_index)}: ${step.statement}`,
  );
  const category = root.category === '' ? '' : label('cause_category', root.category);
  return [
    ...whys,
    `${text('scr11.root_category')}: ${category}`,
    `${text('scr11.root_statement')}: ${root.statement}`,
    `${text('scr11.conclusion')}: ${root.conclusion}`,
  ].join('\n');
}

/** Lo que el servidor sigue esperando tras la firma rechazada: su lista, menos lo enviado. */
function missingAfter(analysis: Analysis, body: AnalysisSignatureBody): string[] {
  const sent: Readonly<Record<string, boolean>> = {
    why_steps: analysis.why_steps.length > 0,
    root_cause_category: body.root_cause_category != null,
    root_cause_statement: body.root_cause_statement != null,
    conclusion: body.conclusion != null,
  };
  return analysis.pending_information.filter((item) => sent[item] !== true);
}

export function Workspace({
  initial,
  resumeRequestId = null,
}: {
  readonly initial: Analysis;
  /** La última solicitud al asistente del análisis retomado: su borrador se vuelve a mostrar. */
  readonly resumeRequestId?: string | null;
}) {
  const clock = rootCauseClock();
  const { principal } = useUiSession();
  const [signer] = useState<SignerProfile>(() => ({
    display_name: principal?.display_name ?? '',
    professional_license: principal?.professional_license ?? null,
  }));
  const [analysis, setAnalysis] = useState(initial);
  const [whys, setWhys] = useState<WhyDraft[]>(() => draftsFrom(initial));
  const [root, setRoot] = useState<RootDraft>(EMPTY_ROOT);
  const [notices, setNotices] = useState<readonly Notice[]>([]);
  const [saving, setSaving] = useState<number | null>(null);
  const [missing, setMissing] = useState<readonly string[] | null>(null);
  const signing = useRef<Promise<void> | null>(null);
  const analysisId = initial.analysis_id;

  const scope = principal?.organization.organization_id ?? 'none';
  const concession = principal?.concession_id ?? null;
  const hierarchy = useScreenQuery({
    key: screenKey({ screen: 'scr11', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    read: ({ signal }) => rootCausePort.hierarchy.read({}, signal),
  });
  const timeZone = plantTimeZone(hierarchy.data, analysis.zone_id) ?? 'UTC';

  const assistant = useAssistantDraft({
    clock,
    request: () => rootCausePort.requestAssistant.write({ path: { analysis_id: analysisId } }),
    status: (request_id, signal) =>
      rootCausePort.assistantStatus.read({ path: { request_id } }, signal),
    classify: assistantFailure,
    resumeRequestId,
  });

  const putStep = useScreenMutation({
    write: (input: { readonly index: number; readonly body: AnalysisStepBody }) =>
      rootCausePort.putStep.write({
        path: { analysis_id: analysisId, step_index: input.index },
        body: input.body,
      }),
    invalidates: { screen: 'scr11', resources: ['root_cause'] },
  });
  const signMutation = useScreenMutation({
    write: (body: AnalysisSignatureBody) =>
      rootCausePort.sign.write({ path: { analysis_id: analysisId }, body }),
    invalidates: { screen: 'scr11', resources: ['root_cause', 'findings'] },
  });

  const setNotice = (field: string, value: Notice | null) => {
    setNotices((current) => [
      ...current.filter((entry) => entry.field !== field),
      ...(value === null ? [] : [value]),
    ]);
  };

  const saveStep = async (offset: number): Promise<void> => {
    const index = offset + 1;
    const field = `why-${String(index)}`;
    const draft = whys[offset] ?? { statement: '', category: '' };
    setSaving(offset);
    setNotice(field, null);
    try {
      const updated = await putStep.run({
        index,
        body: {
          statement: draft.statement,
          evidence_refs: savedStep(analysis, index)?.evidence_refs ?? [],
          category: draft.category === '' ? null : draft.category,
        },
      });
      setAnalysis(updated);
    } catch (error) {
      setNotice(field, notice(field, error));
      throw error;
    } finally {
      setSaving(null);
    }
  };

  /** «Guardar sin terminar»: los porqués con cambios, en orden; el resto ya está en el servidor. */
  const savePartial = async (): Promise<void> => {
    for (let offset = 0; offset < WHY_COUNT; offset += 1) {
      const draft = whys[offset];
      if (draft === undefined || synced(analysis, whys, offset)) continue;
      if (blank(draft.statement) && savedStep(analysis, offset + 1) === undefined) continue;
      await saveStep(offset);
    }
  };

  const sign = (): Promise<void> => {
    if (signing.current !== null) return signing.current;
    const body: AnalysisSignatureBody = {
      professional_license: signer.professional_license,
      root_cause_category: root.category === '' ? null : root.category,
      root_cause_statement: blank(root.statement) ? null : root.statement,
      conclusion: blank(root.conclusion) ? null : root.conclusion,
      cause_effect_branches: [],
    };
    setNotice('signature', null);
    const run = signMutation
      .run(body)
      .then(
        (signed) => {
          setAnalysis(signed);
          setMissing(null);
        },
        (error: unknown) => {
          const value = notice('signature', error);
          setNotice('signature', value);
          if (value.error.detail_code === 'loop_analysis_information_insufficient') {
            setMissing(missingAfter(analysis, body));
          }
          throw error;
        },
      )
      .finally(() => {
        signing.current = null;
      });
    signing.current = run;
    return run;
  };

  const signed = analysis.state === 'signed';
  // Tras `loop_analysis_information_insufficient` el servidor no devuelve el análisis: la lista es
  // la suya de la última respuesta, menos lo que la firma rechazada sí envió.
  const pendingItems =
    missing ?? (analysis.state === 'pending_information' ? analysis.pending_information : null);
  const whyComplete = (offset: number) => {
    const draft = whys[offset];
    if (draft === undefined) return false;
    if (synced(analysis, whys, offset)) return true;
    // Un porqué posterior al primero puede quedar en blanco: el servidor decide el orden.
    return offset > 0 && blank(draft.statement) && savedStep(analysis, offset + 1) === undefined;
  };
  const rootComplete = root.category !== '' && !blank(root.statement) && !blank(root.conclusion);
  const dirty =
    !signed &&
    (whys.some((draft, offset) => !synced(analysis, whys, offset) && !blank(draft.statement)) ||
      !blank(root.statement) ||
      !blank(root.conclusion) ||
      root.category !== '');
  const signatureNotice = noticeForField(notices, 'signature');

  const steps = [
    ...whys.map((draft, offset) => ({
      id: `why-${String(offset + 1)}`,
      title: `${text('scr11.why_title')} ${String(offset + 1)}`,
      complete: whyComplete(offset),
      content: (
        <WhyStep
          analysis={analysis}
          offset={offset}
          draft={draft}
          saving={saving === offset}
          fieldNotice={noticeForField(notices, `why-${String(offset + 1)}`)}
          onChange={(next) => {
            setWhys((current) => current.map((entry, i) => (i === offset ? next : entry)));
          }}
          onSave={() => {
            void saveStep(offset).catch(() => undefined);
          }}
        />
      ),
    })),
    {
      id: 'root',
      title: text('scr11.root_title'),
      complete: rootComplete,
      content: <RootStep root={root} onChange={setRoot} />,
    },
  ];

  const summary = (
    <div className={styles.summary}>
      <SavedWhys analysis={analysis} />
      <SignatureBlock
        principal={signer}
        textToSign={textToSign(analysis, root)}
        notice={signatureNotice}
        pending={signMutation.status === 'pending'}
        onSign={() => {
          void sign().catch(() => undefined);
        }}
      />
      <ValidatorDetail value={signatureNotice} />
    </div>
  );

  return (
    <main className={styles.layout}>
      <h1>{text('navigation.scr11')}</h1>
      <NoticeStack
        notices={notices}
        clock={clock}
        onDismiss={(id) => {
          setNotices((current) => current.filter((entry) => entry.id !== id));
        }}
      />
      <dl className={styles.facts}>
        <dt>{text('scr11.state')}</dt>
        <dd data-part="analysis-state">{label('root_cause_analysis_state', analysis.state)}</dd>
        <dt>{text('scr11.created_at')}</dt>
        <dd>
          <TimestampWithOffset instant={analysis.created_at} timeZone={timeZone} />
        </dd>
        <dt>{text('scr11.updated_at')}</dt>
        <dd>
          <TimestampWithOffset instant={analysis.updated_at} timeZone={timeZone} />
        </dd>
      </dl>
      {pendingItems === null || signed ? null : <PendingInformation items={pendingItems} />}
      <div className={styles.columns}>
        <div className={styles.main}>
          {signed ? (
            <SignedAnalysis analysis={analysis} timeZone={timeZone} />
          ) : (
            <StepWizard
              title={text('scr11.wizard_title')}
              steps={steps}
              summary={summary}
              dirty={dirty}
              serverProgressRef={analysisId}
              onSubmit={sign}
              onSavePartial={savePartial}
            />
          )}
        </div>
        <aside className={styles.side}>
          {signed ? null : (
            <AssistantDraftPanel
              state={assistant.state}
              onRequest={assistant.request}
              onUseDraft={(draft) => {
                setRoot((current) => ({
                  ...current,
                  statement: draft.draft_text,
                  category:
                    current.category === '' &&
                    CAUSE_CATEGORIES.some((value) => value === draft.proposed_cause_category)
                      ? (draft.proposed_cause_category ?? '')
                      : current.category,
                }));
              }}
            />
          )}
          <ZoneContextPanel context={analysis.zone_context} timeZone={timeZone} />
        </aside>
      </div>
    </main>
  );
}
