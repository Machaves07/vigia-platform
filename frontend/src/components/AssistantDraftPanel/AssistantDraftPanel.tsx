/**
 * UI-22 `AssistantDraftPanel` (BR-APP-34, 49; PR-APP-14; P8: la IA conduce el método y el humano
 * decide).
 *
 * - El borrador se muestra **siempre** con la marca «borrador del asistente, requiere revisión»,
 *   sus citas de evidencia y el modelo que lo produjo, sea cual sea `is_draft` en la respuesta.
 * - Su única acción sobre el borrador copia el texto (y la categoría propuesta) a un campo
 *   editable de la pantalla (`draftAccepted`); no existe «aceptar y cerrar» ni ninguna acción que
 *   firme o cierre: firmar es una acción humana separada (UI-16).
 * - `unavailable`, el tope de 2 min o cualquier fallo muestran «asistente no disponible; continúe
 *   con la plantilla guiada»; el panel nunca bloquea ni oculta el flujo guiado.
 * - Un rechazo de los validadores muestra el código y, si la respuesta los trae, el término y la
 *   sugerencia tal cual llegan (`ValidatorRejection`).
 *
 * Presentacional: el estado lo lleva `useAssistantDraft` en la pantalla.
 */
import { useId, useState } from 'react';

import { text } from '@/texts';
import { label } from '@/texts/labels';

import styles from './AssistantDraftPanel.module.css';
import {
  canRequest,
  type AssistantDraft,
  type AssistantPanelState,
  type AssistantRejection,
} from './state';

export interface AssistantDraftPanelProps {
  readonly state: AssistantPanelState;
  /** `requested`: crea una solicitud al asistente. */
  readonly onRequest: () => void;
  /** `draftAccepted`: copia el borrador a un campo editable; nunca firma ni cierra. */
  readonly onUseDraft: (draft: AssistantDraft) => void;
}

/** Código, término y sugerencia de un rechazo de los validadores de U-04, sin inventar nada. */
export function ValidatorRejection({ rejection }: { readonly rejection: AssistantRejection }) {
  const code =
    rejection.code === null
      ? text('components_assistant.rejection.unknown')
      : text(`components_assistant.rejection.${rejection.code}`);
  return (
    <div className={styles.rejection} data-part="rejection">
      <dl className={styles.facts}>
        <dt>{text('components_assistant.rejection.code')}</dt>
        <dd data-part="rejection-code">{code}</dd>
        {rejection.term === null ? null : (
          <>
            <dt>{text('components_assistant.rejection.term')}</dt>
            <dd data-part="rejection-term">{rejection.term}</dd>
          </>
        )}
        {rejection.suggestion === null ? null : (
          <>
            <dt>{text('components_assistant.rejection.suggestion')}</dt>
            <dd data-part="rejection-suggestion">{rejection.suggestion}</dd>
          </>
        )}
      </dl>
      {rejection.term === null && rejection.suggestion === null ? (
        <p data-part="rejection-no-detail">{text('components_assistant.rejection.no_detail')}</p>
      ) : null}
    </div>
  );
}

function Draft({
  draft,
  onUseDraft,
}: {
  readonly draft: AssistantDraft;
  readonly onUseDraft: (draft: AssistantDraft) => void;
}) {
  const [copied, setCopied] = useState(false);
  const mark = text('components_assistant.draft_mark');
  return (
    <article className={styles.draft} aria-label={mark} data-part="draft">
      <p className={styles.mark} data-part="draft-mark">
        <strong>{mark}</strong>
      </p>
      <h3 className={styles.subtitle}>{text('components_assistant.draft_text')}</h3>
      <blockquote className={styles.text} data-part="draft-text">
        {draft.draft_text}
      </blockquote>
      {draft.insufficient_evidence_declared ? (
        <p data-part="insufficient-evidence">
          {text('components_assistant.insufficient_evidence')}
        </p>
      ) : null}
      <dl className={styles.facts}>
        <dt>{text('components_assistant.proposed_category')}</dt>
        <dd data-part="proposed-category">
          {draft.proposed_cause_category === null
            ? text('components_assistant.no_proposed_category')
            : label('cause_category', draft.proposed_cause_category)}
        </dd>
        <dt>{text('components_assistant.model')}</dt>
        <dd data-part="model">{`${draft.model} ${draft.model_version}`}</dd>
      </dl>
      <h3 className={styles.subtitle}>{text('components_assistant.citations')}</h3>
      {draft.citations.length === 0 ? (
        <p data-part="no-citations">{text('components_assistant.no_citations')}</p>
      ) : (
        <ul className={styles.citations} aria-label={text('components_assistant.citations')}>
          {draft.citations.map((citation, index) => (
            <li key={`${citation.id}-${String(index)}`} data-part="citation">
              <span data-part="citation-kind">{label('evidence_ref_kind', citation.kind)}</span>
              {': '}
              <q data-part="citation-fact">{citation.quoted_fact_es}</q>{' '}
              <span>
                ({text('components_assistant.citation_alias')}{' '}
                <code data-part="citation-alias">{citation.id}</code>)
              </span>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        className={styles.button}
        onClick={() => {
          onUseDraft(draft);
          setCopied(true);
        }}
      >
        {text('components_assistant.use_draft')}
      </button>
      {copied ? <p role="status">{text('components_assistant.draft_copied')}</p> : null}
    </article>
  );
}

export function AssistantDraftPanel({ state, onRequest, onUseDraft }: AssistantDraftPanelProps) {
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId} data-status={state.status}>
      <h2 id={headingId} className={styles.title}>
        {text('components_assistant.title')}
      </h2>
      <p>{text('components_assistant.intro')}</p>
      <button
        type="button"
        className={styles.button}
        disabled={!canRequest(state)}
        onClick={onRequest}
      >
        {text('components_assistant.request')}
      </button>
      <p role="status" data-part="assistant-progress">
        {state.status === 'requesting'
          ? text('components_assistant.requesting')
          : state.status === 'polling'
            ? text('components_assistant.polling')
            : ''}
      </p>
      {state.status === 'ready' ? (
        <Draft key={state.requestId} draft={state.draft} onUseDraft={onUseDraft} />
      ) : null}
      {state.status === 'unavailable' ? (
        <div className={styles.notice} data-part="assistant-unavailable">
          <p>
            <strong>{text('components_assistant.unavailable')}</strong>
          </p>
          {state.expired ? <p data-part="expired">{text('components_assistant.expired')}</p> : null}
          {state.cause === null ? null : (
            <p data-part="unavailable-cause">
              {text('components_assistant.unavailable_cause')}:{' '}
              {label('assistant_unavailable_cause', state.cause)}
            </p>
          )}
        </div>
      ) : null}
      {state.status === 'rejected' ? (
        <div className={styles.notice} data-part="assistant-rejected">
          <p>
            <strong>{text('components_assistant.rejected')}</strong>
          </p>
          <ValidatorRejection rejection={state.rejection} />
        </div>
      ) : null}
    </section>
  );
}
