/**
 * Acta de comisionamiento cerrada (IF-13 paso 7; `CommissioningRecordOut`, DE §2.15): la matriz con
 * sus resultados, los falsos negativos y la tasa de falsas alarmas frente al umbral con su
 * aceptación, las tasas medidas por cámara, la latencia por tramo con el reloj que la midió, las
 * mediciones del instalador, la oclusión por cámara, las horas por tipo de paso y el total de la
 * zona, y las firmas. El acta no trae responsables y la vista no los construye (H-53).
 *
 * Las sumas de latencia son orientativas (P6): se presentan marcadas, nunca como cifra prometida.
 * El documento legible se abre por navegación (generado a demanda, nunca en caché).
 */
import { useId, type ReactNode } from 'react';

import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import { HoursPanel } from '@/pages/scr06/session/HoursPanel';
import {
  recordDocumentHref,
  type CommissioningRecord,
  type LatencyTranche,
} from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './record.module.css';

/** Los cuatro tramos de latencia, en el orden del acta. */
const TRANCHES = [
  'node_tranche',
  'platform_tranche',
  'exposure_tranche',
  'served_tranche',
] as const;

/** Relojes de un tramo (`MeasuredBy` de U-03) con su texto. */
const MEASURED_BY: ReadonlySet<string> = new Set(['installer', 'platform', 'browser', 'node']);

const RATE = new Intl.NumberFormat('es-ES', { maximumFractionDigits: 4 });

function measuredBy(value: string): string {
  return MEASURED_BY.has(value)
    ? text(`scr06.record.measured_by.${value}`)
    : text('common.unknown');
}

function trancheLabel(value: string): string {
  return (TRANCHES as readonly string[]).includes(value)
    ? text(`scr06.record.latency.${value}`)
    : text('common.unknown');
}

function milliseconds(value: number | null | undefined): string {
  return value === null || value === undefined
    ? text('scr06.record.not_measured')
    : `${String(value)} ${text('scr06.record.ms')}`;
}

function Section({ title, children }: { readonly title: string; readonly children: ReactNode }) {
  const headingId = useId();
  return (
    <section className={styles.section} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.subtitle}>
        {title}
      </h3>
      {children}
    </section>
  );
}

function Tranche({
  name,
  tranche,
}: {
  readonly name: string;
  readonly tranche: LatencyTranche | null;
}) {
  return (
    <tr data-part="latency_tranche" data-tranche={name}>
      <th scope="row">{trancheLabel(name)}</th>
      {tranche === null ? (
        <td colSpan={5} data-part="tranche_not_measured">
          {text('scr06.record.not_measured')}
        </td>
      ) : (
        <>
          <td data-part="p95_ms">{milliseconds(tranche.p95_ms)}</td>
          <td>{milliseconds(tranche.median_ms)}</td>
          <td>{milliseconds(tranche.max_ms)}</td>
          <td>{tranche.repetitions ?? text('scr06.record.not_measured')}</td>
          <td data-part="measured_by">{measuredBy(tranche.measured_by)}</td>
        </>
      )}
    </tr>
  );
}

export function RecordView({ record }: { readonly record: CommissioningRecord }) {
  const headingId = useId();
  const documentHref = recordDocumentHref(record.commissioning_record_id);
  const { latency, installer_measurements: installer } = record;
  const baselines = installer.baselines ?? [];
  return (
    <article className={styles.record} aria-labelledby={headingId} data-part="commissioning_record">
      <h2 id={headingId} className={styles.title}>
        {text('scr06.record.title')}
      </h2>
      <dl className={styles.facts}>
        <dt>{text('scr06.session.kind')}</dt>
        <dd data-part="record_kind">{label('walk_test_kind', record.kind)}</dd>
        <dt>{text('scr06.session.catalog_version')}</dt>
        <dd>{record.catalog_version}</dd>
        <dt>{text('scr06.session.passes_per_cell')}</dt>
        <dd>{record.passes_per_cell}</dd>
        <dt>{text('scr06.record.closed_at')}</dt>
        <dd>
          <ServerTimestamp instant={record.closed_at} />
        </dd>
      </dl>
      {documentHref === null ? null : (
        <p className={styles.document}>
          <a
            href={documentHref}
            target="_blank"
            rel="noopener noreferrer"
            data-part="record_document"
          >
            {text('scr06.record.document')}
          </a>
        </p>
      )}

      <Section title={text('scr06.record.matrix.title')}>
        <table className={styles.table} data-part="matrix_results">
          <thead>
            <tr>
              <th scope="col">{text('scr06.matrix.row')}</th>
              <th scope="col">{text('scr06.record.matrix.detected')}</th>
              <th scope="col">{text('scr06.record.matrix.missed')}</th>
              <th scope="col">{text('scr06.record.matrix.false_alarms')}</th>
              <th scope="col">{text('scr06.record.matrix.unverifiable')}</th>
            </tr>
          </thead>
          <tbody>
            {record.matrix_results.map((row) => (
              <tr key={row.row_id} data-part="matrix_result">
                <th scope="row">{row.row_id}</th>
                <td>{row.detected}</td>
                <td>{row.missed}</td>
                <td>{row.false_alarms}</td>
                <td>{(row.unverifiable_evidence_refs ?? []).length}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <dl className={styles.facts}>
          <dt>{text('scr06.record.false_negatives_total')}</dt>
          <dd data-part="false_negatives_total">{record.false_negatives_total}</dd>
          <dt>{text('scr06.record.false_alarm_rate')}</dt>
          <dd data-part="false_alarm_rate">{RATE.format(record.false_alarm_rate_observed)}</dd>
          <dt>{text('scr06.record.false_alarm_threshold')}</dt>
          <dd data-part="false_alarm_threshold">{RATE.format(record.false_alarm_threshold)}</dd>
        </dl>
        {record.false_alarm_acceptance === null ? null : (
          <dl className={styles.facts} data-part="false_alarm_acceptance">
            <dt>{text('scr06.record.acceptance.reason')}</dt>
            <dd>{record.false_alarm_acceptance.reason_es}</dd>
            <dt>{text('scr06.record.acceptance.accepted_at')}</dt>
            <dd>
              <ServerTimestamp instant={record.false_alarm_acceptance.accepted_at} />
            </dd>
          </dl>
        )}
      </Section>

      <Section title={text('scr06.record.cameras.title')}>
        {record.cameras_measured.length === 0 ? (
          <p className={styles.empty}>{text('scr06.record.cameras.none')}</p>
        ) : (
          <table className={styles.table} data-part="cameras_measured">
            <thead>
              <tr>
                <th scope="col">{text('scr06.record.camera')}</th>
                <th scope="col">{text('scr06.record.cameras.measured_fps')}</th>
                <th scope="col">{text('scr06.record.cameras.declared_min_fps')}</th>
              </tr>
            </thead>
            <tbody>
              {record.cameras_measured.map((camera) => (
                <tr key={camera.camera_id} data-part="camera_rate">
                  <th scope="row">{camera.camera_id}</th>
                  <td>
                    {camera.measured_fps === null
                      ? text('scr06.record.not_measured')
                      : RATE.format(camera.measured_fps)}
                  </td>
                  <td>
                    {camera.declared_min_fps === null
                      ? text('scr06.record.not_declared')
                      : RATE.format(camera.declared_min_fps)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title={text('scr06.record.latency.title')}>
        <table className={styles.table} data-part="latency">
          <thead>
            <tr>
              <th scope="col">{text('scr06.record.latency.tranche')}</th>
              <th scope="col">{text('scr06.record.latency.p95')}</th>
              <th scope="col">{text('scr06.record.latency.median')}</th>
              <th scope="col">{text('scr06.record.latency.max')}</th>
              <th scope="col">{text('scr06.record.latency.repetitions')}</th>
              <th scope="col">{text('scr06.record.latency.measured_by')}</th>
            </tr>
          </thead>
          <tbody>
            {TRANCHES.map((name) => (
              <Tranche key={name} name={name} tranche={latency[name] ?? null} />
            ))}
          </tbody>
        </table>
        {(latency.not_measured ?? []).length === 0 ? null : (
          <p data-part="latency_not_measured">
            {`${text('scr06.record.latency.not_measured')}: ${(latency.not_measured ?? [])
              .map(trancheLabel)
              .join(', ')}`}
          </p>
        )}
        <dl className={styles.facts} data-part="latency_indicative">
          <dt>{text('scr06.record.latency.indicative_p95')}</dt>
          <dd>{milliseconds(latency.indicative_sum_p95_ms)}</dd>
          <dt>{text('scr06.record.latency.indicative_median')}</dt>
          <dd>{milliseconds(latency.indicative_sum_median_ms)}</dd>
        </dl>
        <p className={styles.note}>{text('scr06.record.latency.indicative_note')}</p>
      </Section>

      <Section title={text('scr06.record.installer.title')}>
        <dl className={styles.facts} data-part="installer_measurements">
          <dt>{text('scr06.close.measurements.beacon')}</dt>
          <dd data-part="beacon_latency">{milliseconds(installer.beacon_latency_ms_p95)}</dd>
          <dt>{text('scr06.record.latency.measured_by')}</dt>
          <dd>{measuredBy(installer.measured_by)}</dd>
        </dl>
        {baselines.length === 0 ? (
          <p className={styles.empty}>{text('scr06.record.installer.no_baselines')}</p>
        ) : (
          <ul className={styles.list} data-part="baselines">
            {baselines.map((baseline) => (
              <li key={`${baseline.camera_id}-${baseline.captured_at}`}>
                {`${text('scr06.record.camera')}: ${baseline.camera_id} · `}
                <ServerTimestamp instant={baseline.captured_at} />
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={text('scr06.record.occlusion.title')}>
        {record.occlusion_summary.length === 0 ? (
          <p className={styles.empty}>{text('scr06.occlusion.list.empty')}</p>
        ) : (
          <ul className={styles.list} data-part="occlusion_summary">
            {record.occlusion_summary.map((entry) => (
              <li key={entry.camera_id} data-verification={entry.verification}>
                {`${text('scr06.record.camera')}: ${entry.camera_id} · ${label('occlusion_verification', entry.verification)}`}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <HoursPanel session={record} />

      <Section title={text('scr06.record.signatures.title')}>
        <ul className={styles.list} data-part="signatures">
          {record.signatures.map((signature) => (
            <li key={signature.user_id}>
              {`${signature.user_id} · ${label('role', signature.role_in_use)} · `}
              <ServerTimestamp instant={signature.signed_at} />
            </li>
          ))}
        </ul>
      </Section>
    </article>
  );
}
