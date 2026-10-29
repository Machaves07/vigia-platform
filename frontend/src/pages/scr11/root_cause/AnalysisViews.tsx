/**
 * Vistas de lectura del análisis (RV-15): los porqués guardados, «pendiente de información» con
 * lo que falta y el análisis firmado. Toda lista cerrada se muestra con su etiqueta; ninguna
 * causa se atribuye a una persona (las tres categorías son proceso, ingeniería y organización).
 */
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { Analysis } from '@/ports/scr11RootCause';
import { text } from '@/texts';
import { label, labelInline } from '@/texts/labels';

import styles from './root_cause.module.css';

export function SavedWhys({ analysis }: { readonly analysis: Analysis }) {
  return (
    <>
      <h4>{text('scr11.summary_whys')}</h4>
      {analysis.why_steps.length === 0 ? (
        <p>{text('scr11.no_whys')}</p>
      ) : (
        <ol data-part="summary-whys">
          {analysis.why_steps.map((step) => (
            <li key={step.step_index}>
              {step.statement}
              {step.category === null ? null : ` (${labelInline('cause_category', step.category)})`}
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

export function PendingInformation({ items }: { readonly items: readonly string[] }) {
  const title = text('scr11.pending_title');
  return (
    <section className={styles.pending} aria-label={title} data-part="pending-information">
      <h2>{title}</h2>
      {items.length === 0 ? (
        <p data-part="missing-unknown">{text('scr11.pending_unknown')}</p>
      ) : (
        <>
          <p>{text('scr11.pending_intro')}</p>
          <ul>
            {items.map((item) => (
              <li key={item} data-part="missing-item">
                {label('analysis_missing_item', item)}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function SignedAnalysis({
  analysis,
  timeZone,
}: {
  readonly analysis: Analysis;
  readonly timeZone: string;
}) {
  const title = text('scr11.signed_title');
  return (
    <section className={styles.signed} aria-label={title} data-part="signed">
      <h2>{title}</h2>
      <p>{text('scr11.signed_readonly')}</p>
      <dl className={styles.facts}>
        <dt>{text('scr11.signed_by')}</dt>
        <dd data-part="signer">
          {analysis.signer === null
            ? text('common.unknown')
            : `${analysis.signer.display_name} · ${analysis.signer.professional_license}`}
        </dd>
        <dt>{text('scr11.signed_at')}</dt>
        <dd>
          {analysis.signed_at === null ? (
            text('common.unknown')
          ) : (
            <TimestampWithOffset instant={analysis.signed_at} timeZone={timeZone} />
          )}
        </dd>
        <dt>{text('scr11.record')}</dt>
        <dd data-part="record">{analysis.record_id ?? text('common.unknown')}</dd>
        <dt>{text('scr11.root_category')}</dt>
        <dd>
          {analysis.root_cause_category === null
            ? text('common.unknown')
            : label('cause_category', analysis.root_cause_category)}
        </dd>
        <dt>{text('scr11.root_statement')}</dt>
        <dd>{analysis.root_cause_statement ?? text('common.unknown')}</dd>
        <dt>{text('scr11.conclusion')}</dt>
        <dd>{analysis.conclusion ?? text('common.unknown')}</dd>
      </dl>
    </section>
  );
}
