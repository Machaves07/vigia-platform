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

/**
 * Los porqués guardados, en el orden del servidor, cada uno con su categoría etiquetada si la
 * tiene. `heading` es el nivel de su título: `h4` en el resumen de la plantilla y `h3` dentro del
 * análisis firmado, que va bajo un `h2`.
 */
export function SavedWhys({
  analysis,
  heading = 'h4',
}: {
  readonly analysis: Analysis;
  readonly heading?: 'h3' | 'h4';
}) {
  const Heading = heading;
  return (
    <>
      <Heading>{text('scr11.summary_whys')}</Heading>
      {analysis.why_steps.length === 0 ? (
        <p data-part="no-whys">{text('scr11.no_whys')}</p>
      ) : (
        <ol data-part="summary-whys">
          {analysis.why_steps.map((step) => (
            <li key={step.step_index} data-part="why">
              <span data-part="why-statement">{step.statement}</span>
              {step.category === null ? null : (
                <>
                  {' ('}
                  <span data-part="why-category" data-value={step.category}>
                    {labelInline('cause_category', step.category)}
                  </span>
                  {')'}
                </>
              )}
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

/**
 * El análisis firmado (O-10, TASK-584): quién y cuándo firmó, después la cadena de porqués que
 * llevó a la causa (lo más útil para un auditor o el COPASST) y, al final, la categoría de la causa
 * raíz, su enunciado y la conclusión. Solo lectura: no hay botones de editar ni de borrar.
 */
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
      <dl className={styles.facts} data-part="signed-facts">
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
      </dl>
      <SavedWhys analysis={analysis} heading="h3" />
      <h3>{text('scr11.summary_root')}</h3>
      <dl className={styles.facts} data-part="signed-root">
        <dt>{text('scr11.root_category')}</dt>
        <dd data-part="root-category">
          {analysis.root_cause_category === null
            ? text('common.unknown')
            : label('cause_category', analysis.root_cause_category)}
        </dd>
        <dt>{text('scr11.root_statement')}</dt>
        <dd data-part="root-statement">
          {analysis.root_cause_statement ?? text('common.unknown')}
        </dd>
        <dt>{text('scr11.conclusion')}</dt>
        <dd data-part="conclusion">{analysis.conclusion ?? text('common.unknown')}</dd>
      </dl>
    </section>
  );
}
