/**
 * Pruebas de oclusión de la sesión (IF-13 paso 5): por cámara, la ventana, la verificación del
 * servidor (`verified`, `declared`, `failed` o `pending`) con su etiqueta, el plazo de las que
 * siguen verificándose y, en las fallidas, la causa explicada (`no_observability_events_in_window`
 * o `redundancy_not_verified`). Nada se deduce en el navegador: la verificación es la del servidor.
 */
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import type { OcclusionTest } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './occlusion.module.css';

/** Causas de fallo que la pantalla explica (las de `OcclusionTestOut.failure_reason`). */
const FAILURE_EXPLANATIONS: Readonly<Record<string, string>> = {
  no_observability_events_in_window: 'scr06.occlusion.failure.no_observability_events_in_window',
  redundancy_not_verified: 'scr06.occlusion.failure.redundancy_not_verified',
};

function FailureReason({ reason }: { readonly reason: string }) {
  const explanation = FAILURE_EXPLANATIONS[reason];
  return (
    <>
      <dt>{text('scr06.occlusion.list.failure')}</dt>
      <dd data-part="failure_reason" data-failure-reason={reason}>
        <p className={styles.explanation}>{label('catalog_detail_code', `catalog_${reason}`)}</p>
        {explanation === undefined ? null : (
          <p className={styles.explanation} data-text-key={explanation}>
            {text(explanation)}
          </p>
        )}
      </dd>
    </>
  );
}

export interface OcclusionListProps {
  readonly tests: readonly OcclusionTest[];
  /** Copia la cámara y la ventana de una prueba pendiente al formulario para declararla. */
  readonly onDeclare?: (test: OcclusionTest) => void;
}

export function OcclusionList({ tests, onDeclare }: OcclusionListProps) {
  return (
    <section className={styles.panel} aria-label={text('scr06.occlusion.list.title')}>
      <h2 className={styles.title}>{text('scr06.occlusion.list.title')}</h2>
      {tests.length === 0 ? (
        <p className={styles.empty} data-part="occlusion_empty">
          {text('scr06.occlusion.list.empty')}
        </p>
      ) : (
        <ul className={styles.list}>
          {tests.map((test) => (
            <li
              key={test.test_id}
              className={styles.test}
              data-part="occlusion_test"
              data-occlusion-test={test.test_id}
              data-verification={test.verification}
            >
              <dl className={styles.facts}>
                <dt>{text('scr06.occlusion.list.camera')}</dt>
                <dd data-part="camera_id">{test.camera_id}</dd>
                <dt>{text('scr06.occlusion.list.started_at')}</dt>
                <dd>
                  <ServerTimestamp instant={test.started_at} />
                </dd>
                <dt>{text('scr06.occlusion.list.ended_at')}</dt>
                <dd>
                  <ServerTimestamp instant={test.ended_at} />
                </dd>
                <dt>{text('scr06.occlusion.list.verification')}</dt>
                <dd data-part="verification">
                  {label('occlusion_verification', test.verification)}
                </dd>
                {test.verification === 'pending' ? (
                  <>
                    <dt>{text('scr06.occlusion.list.deadline')}</dt>
                    <dd data-part="deadline">
                      <ServerTimestamp instant={test.deadline} />
                    </dd>
                  </>
                ) : null}
                <dt>{text('scr06.occlusion.list.events')}</dt>
                <dd data-part="correlated_events">{test.correlated_event_ids.length}</dd>
                {test.declared_reason_es === null ? null : (
                  <>
                    <dt>{text('scr06.occlusion.list.declared_reason')}</dt>
                    <dd data-part="declared_reason">{test.declared_reason_es}</dd>
                  </>
                )}
                {test.verification === 'failed' && test.failure_reason !== null ? (
                  <FailureReason reason={test.failure_reason} />
                ) : null}
              </dl>
              {test.verification === 'pending' ? (
                <>
                  <p className={styles.pending} role="status" data-part="pending">
                    {text('scr06.occlusion.list.pending')}
                  </p>
                  {onDeclare === undefined ? null : (
                    <div className={styles.actions}>
                      <button
                        type="button"
                        className={styles.secondary}
                        onClick={() => {
                          onDeclare(test);
                        }}
                      >
                        {text('scr06.occlusion.list.declare')}
                      </button>
                    </div>
                  )}
                </>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
