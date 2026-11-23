/**
 * Área de trabajo de un análisis iniciado (IF-10; BR-APP-34, 43, 45, 47, 49).
 *
 * - UI-15 con los porqués guardados más el siguiente (como mucho cinco; cada uno se guarda en el
 *   servidor con SUP-U05-17) y la causa raíz con su categoría cerrada, el enunciado y la
 *   conclusión (se envían al firmar). El siguiente porqué en blanco lleva a la causa raíz
 *   (TASK-581, `whySteps.ts`). «Siguiente» con un porqué sin guardar lo guarda (texto y categoría
 *   en pantalla) y avanza solo si el servidor lo acepta. Nunca hay dos PUT del mismo porqué a
 *   la vez: lo mismo que ya va en vuelo espera ese guardado; algo distinto sale cuando termine. «Guardar sin terminar» existe porque hay
 *   `server_progress_ref` (el `analysis_id`) y guarda los porqués con cambios.
 * - Cada texto es UI-13 con los límites de U-04 (`ROOT_CAUSE_TEXT_LIMITS`): contador, espejo de la
 *   política base antes de enviar y recorte de los extremos (A-126). Con un error local no se
 *   envía nada; el orden de los porqués, los validadores y la licencia los decide el servidor: sus
 *   rechazos se anclan al campo (`loop_analysis_step_out_of_order`, `loop_blocked_vocabulary_term`,
 *   `loop_person_attribution_detected`, `loop_cause_category_not_allowed`,
 *   `loop_signer_license_missing`) y nada se deshabilita por una regla propia (BR-APP-43).
 * - UI-22 va al lado de la plantilla: su estado nunca la oculta ni la bloquea (PR-APP-14).
 * - UI-16 firma con SUP-U05-18; `loop_analysis_information_insufficient` deja el análisis
 *   «pendiente de información» con lo que falta y `409 loop_transition_not_allowed` (diferencia 9)
 *   se muestra anclado. «Enviar» del resumen y «Firmar» envían la misma y única firma.
 */
import { useId, useRef, useState } from 'react';

import { AssistantDraftPanel } from '@/components/AssistantDraftPanel/AssistantDraftPanel';
import { useAssistantDraft } from '@/components/AssistantDraftPanel/useAssistantDraft';
import { NoticeStack } from '@/components/NoticeStack/NoticeStack';
import { noticeForField, type Notice } from '@/components/NoticeStack/notice';
import {
  ReasonField,
  reasonIssueKey,
  reasonVerdict,
  submittedText,
} from '@/components/ReasonField/ReasonField';
import { TextNotice, textRejection } from '@/components/ReasonField/TextNotice';
import { SignatureBlock, type SignerProfile } from '@/components/SignatureBlock/SignatureBlock';
import { StepWizard } from '@/components/StepWizard/StepWizard';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import {
  assistantFailure,
  noticeError,
  plantTimeZone,
  ROOT_CAUSE_TEXT_LIMITS,
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
import {
  matches,
  savedStep,
  synced,
  visibleWhyCount,
  WHY_COUNT,
  whyComplete,
  whyHasChanges,
  whyOptional,
  type WhyDraft,
} from './whySteps';
import { ZoneContextPanel } from './ZoneContextPanel';

/** Las tres categorías cerradas de causa (proceso, ingeniería, organización). */
const CAUSE_CATEGORIES = ['process', 'engineering', 'organization'] as const;

interface RootDraft {
  readonly category: string;
  readonly statement: string;
  readonly conclusion: string;
}

const EMPTY_ROOT: RootDraft = { category: '', statement: '', conclusion: '' };
const EMPTY_WHY: WhyDraft = { statement: '', category: '' };

/** Un guardado de un porqué en vuelo: qué pide, su promesa y el análisis que deja (o `null`). */
interface StepSave {
  readonly key: string;
  readonly run: Promise<void>;
  readonly settled: Promise<Analysis | null>;
}

/** Lo que pide guardar un porqué: el texto que se enviaría (recortado, en NFC) y su categoría. */
function requestKey(draft: WhyDraft): string {
  return JSON.stringify([submittedText(draft.statement).normalize('NFC'), draft.category]);
}
const LOOP_TEXT = { loopText: true } as const;

function whyOptions(analysis: Analysis, offset: number) {
  return { loopText: true, optional: whyOptional(analysis, offset) };
}

function draftsFrom(analysis: Analysis): WhyDraft[] {
  return Array.from({ length: WHY_COUNT }, (_, offset) => {
    const saved = savedStep(analysis, offset + 1);
    return { statement: saved?.statement ?? '', category: saved?.category ?? '' };
  });
}

function blank(value: string): boolean {
  return value.trim() === '';
}

function notice(field: string, error: unknown): Notice {
  return { id: field, field, error: noticeError(error) };
}

function WhyStep({
  analysis,
  offset,
  draft,
  onChange,
  onSave,
  saving,
  showErrors,
  fieldNotice,
}: {
  readonly analysis: Analysis;
  readonly offset: number;
  readonly draft: WhyDraft;
  readonly onChange: (draft: WhyDraft) => void;
  readonly onSave: () => void;
  readonly saving: boolean;
  readonly showErrors: boolean;
  readonly fieldNotice: Notice | undefined;
}) {
  const id = useId();
  const index = offset + 1;
  const saved = savedStep(analysis, index);
  const question =
    saved?.question_es ?? (analysis.next_step_index === index ? analysis.next_question_es : null);
  const isSynced = matches(saved, draft);
  return (
    <div className={styles.step} data-part={`why-${String(index)}`}>
      <p data-part="question">
        <strong>{text('scr11.question')}:</strong> {question ?? text('scr11.question_pending')}
      </p>
      {whyOptional(analysis, offset) ? (
        <p className={styles.hint} data-part="optional-hint" data-text-key="scr11.optional_step">
          {text('scr11.optional_step')}
        </p>
      ) : null}
      <ReasonField
        label={`${text('scr11.statement')} ${String(index)}`}
        name={`why-${String(index)}`}
        value={draft.statement}
        limits={ROOT_CAUSE_TEXT_LIMITS.why_statement}
        {...whyOptions(analysis, offset)}
        showErrors={showErrors}
        notice={fieldNotice}
        onChange={(statement) => {
          onChange({ ...draft, statement });
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
    </div>
  );
}

function RootStep({
  root,
  onChange,
  showErrors,
}: {
  readonly root: RootDraft;
  readonly onChange: (root: RootDraft) => void;
  readonly showErrors: boolean;
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
      <ReasonField
        label={text('scr11.root_statement')}
        name="root_cause_statement"
        value={root.statement}
        limits={ROOT_CAUSE_TEXT_LIMITS.root_cause_statement}
        loopText
        showErrors={showErrors}
        onChange={(statement) => {
          onChange({ ...root, statement });
        }}
      />
      <ReasonField
        label={text('scr11.conclusion')}
        name="conclusion"
        value={root.conclusion}
        limits={ROOT_CAUSE_TEXT_LIMITS.conclusion}
        loopText
        showErrors={showErrors}
        onChange={(conclusion) => {
          onChange({ ...root, conclusion });
        }}
      />
    </div>
  );
}

/** Lo que se envía de un texto de la causa raíz: en blanco, `null` (el servidor dice qué falta). */
function rootText(value: string): string | null {
  return blank(value) ? null : submittedText(value);
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
    `${text('scr11.root_statement')}: ${rootText(root.statement) ?? ''}`,
    `${text('scr11.conclusion')}: ${rootText(root.conclusion) ?? ''}`,
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

/** Un texto escrito de la causa raíz que no pasa el espejo: la firma no sale (A-126). */
function rootInvalid(root: RootDraft): boolean {
  return (['statement', 'conclusion'] as const).some(
    (field) =>
      !blank(root[field]) &&
      reasonIssueKey(
        root[field],
        ROOT_CAUSE_TEXT_LIMITS[field === 'statement' ? 'root_cause_statement' : 'conclusion'],
        LOOP_TEXT,
      ) !== null,
  );
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
  /** Porqués con un intento de guardar y causa raíz con un intento de firmar: muestran su error. */
  const [whyErrors, setWhyErrors] = useState<readonly number[]>([]);
  const [rootErrors, setRootErrors] = useState(false);
  const signing = useRef<Promise<void> | null>(null);
  /** El último guardado pedido de cada porqué mientras sigue en vuelo, por `offset`. */
  const inFlight = useRef(new Map<number, StepSave>());
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

  /** Un PUT del porqué `offset` con `draft`, sobre `base`; devuelve el análisis que queda. */
  const writeStep = async (offset: number, base: Analysis, draft: WhyDraft): Promise<Analysis> => {
    const index = offset + 1;
    const field = `why-${String(index)}`;
    const verdict = reasonVerdict(
      draft.statement,
      ROOT_CAUSE_TEXT_LIMITS.why_statement,
      whyOptions(base, offset),
    );
    setNotice(field, null);
    // Con un error local no hay petición: el error queda en el campo (A-126). Un porqué opcional
    // en blanco no tiene nada que guardar.
    if (!verdict.ok) {
      setWhyErrors((current) => (current.includes(offset) ? current : [...current, offset]));
      throw new Error('El porqué no pasa la validación local.');
    }
    if (verdict.sent === null) return base;
    setSaving(offset);
    try {
      const updated = await putStep.run({
        index,
        body: {
          statement: verdict.sent,
          evidence_refs: savedStep(base, index)?.evidence_refs ?? [],
          category: draft.category === '' ? null : draft.category,
        },
      });
      setAnalysis(updated);
      return updated;
    } catch (error) {
      setNotice(field, notice(field, error));
      throw error;
    } finally {
      setSaving(null);
    }
  };

  /**
   * Guarda el porqué `offset` con lo que hay en pantalla («Guardar porqué», «Siguiente» o «Guardar
   * sin terminar»). Nunca hay dos PUT del mismo paso a la vez: si ya va uno en vuelo con el mismo
   * texto recortado y la misma categoría, devuelve esa promesa; si lo pedido es distinto, lo envía
   * cuando aquel termine (acepte o rechace), sobre el análisis que deje, para que quede lo último.
   */
  const saveStep = (offset: number): Promise<void> => {
    const draft = whys[offset] ?? EMPTY_WHY;
    const key = requestKey(draft);
    const pending = inFlight.current.get(offset);
    if (pending?.key === key) return pending.run;
    const written =
      pending === undefined
        ? writeStep(offset, analysis, draft)
        : pending.settled.then((base) => writeStep(offset, base ?? analysis, draft));
    const entry: StepSave = {
      key,
      run: written
        .then(() => undefined)
        .finally(() => {
          // Solo se borra a sí mismo: si detrás ya va otro guardado del paso, ese sigue en vuelo.
          if (inFlight.current.get(offset) === entry) inFlight.current.delete(offset);
        }),
      settled: written.then(
        (updated) => updated,
        () => null,
      ),
    };
    inFlight.current.set(offset, entry);
    return entry.run;
  };

  /** «Guardar sin terminar»: los porqués con cambios, en orden; el resto ya está en el servidor. */
  const savePartial = async (): Promise<void> => {
    for (let offset = 0; offset < WHY_COUNT; offset += 1) {
      if (!whyHasChanges(analysis, whys, offset)) continue;
      await saveStep(offset);
    }
  };

  const sign = (): Promise<void> => {
    if (signing.current !== null) return signing.current;
    if (rootInvalid(root)) {
      setRootErrors(true);
      return Promise.reject(new Error('La causa raíz no pasa la validación local.'));
    }
    const body: AnalysisSignatureBody = {
      professional_license: signer.professional_license,
      root_cause_category: root.category === '' ? null : root.category,
      root_cause_statement: rootText(root.statement),
      conclusion: rootText(root.conclusion),
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
  const rootComplete = root.category !== '' && !blank(root.statement) && !blank(root.conclusion);
  const dirty =
    !signed &&
    (whys.some((draft, offset) => !synced(analysis, whys, offset) && !blank(draft.statement)) ||
      !blank(root.statement) ||
      !blank(root.conclusion) ||
      root.category !== '');
  const signatureNotice = noticeForField(notices, 'signature');
  // Un rechazo de los textos de la causa raíz dice su motivo real junto a la firma (A-126).
  const textNotice =
    signatureNotice !== undefined && textRejection(signatureNotice) ? signatureNotice : undefined;

  // Los porqués guardados más el siguiente: nunca uno que el servidor rechazaría por fuera de orden.
  const steps = [
    ...whys.slice(0, visibleWhyCount(analysis)).map((draft, offset) => ({
      id: `why-${String(offset + 1)}`,
      title: `${text('scr11.why_title')} ${String(offset + 1)}`,
      complete: whyComplete(analysis, whys, offset),
      // «Siguiente» con cambios sin guardar los guarda (texto y categoría en pantalla) y avanza
      // solo si el servidor los acepta.
      onNext: whyHasChanges(analysis, whys, offset) ? () => saveStep(offset) : undefined,
      content: (
        <WhyStep
          analysis={analysis}
          offset={offset}
          draft={draft}
          saving={saving === offset}
          showErrors={whyErrors.includes(offset)}
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
      content: <RootStep root={root} onChange={setRoot} showErrors={rootErrors} />,
    },
  ];

  const summary = (
    <div className={styles.summary}>
      <SavedWhys analysis={analysis} />
      {rootErrors && rootInvalid(root) ? (
        <p
          role="alert"
          data-part="local_error"
          data-text-key="components_forms.reason.fix_before_send"
        >
          {text('components_forms.reason.fix_before_send')}
        </p>
      ) : null}
      <SignatureBlock
        principal={signer}
        textToSign={textToSign(analysis, root)}
        notice={textNotice === undefined ? signatureNotice : undefined}
        pending={signMutation.status === 'pending'}
        onSign={() => {
          void sign().catch(() => undefined);
        }}
      />
      {textNotice !== undefined ? (
        <TextNotice
          id="signature-text"
          notice={textNotice}
          written={[root.statement, root.conclusion].join('\n')}
          loopText
        />
      ) : null}
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
