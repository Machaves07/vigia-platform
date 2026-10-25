/**
 * Matriz de pases (IF-13 paso 3, RV-09): una fila por postura × estado de señal con el avance y
 * los pases requeridos (mínimo 3 por celda, `passes_per_cell` de la sesión), y la lista de pases
 * registrados con su resultado, su momento y la evidencia elegida.
 *
 * Sin `onSelectRow` la matriz es de solo lectura: es la forma en que la vista RV-09 la pinta en
 * las metapropiedades de PR-APP-07 y PR-APP-10. La pantalla no calcula ninguna tasa: muestra los
 * conteos del servidor y, a lo sumo, su suma como avance de la fila.
 */
import { useId } from 'react';

import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import type { WalkTestSession } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { conditionsText } from './conditions';
import type { ExposureOutcome } from './exposure';
import styles from './matrix.module.css';

export interface PassMatrixTableProps {
  readonly session: WalkTestSession;
  /** Fila elegida para registrar el pase; sin `onSelectRow` no se ofrecen acciones. */
  readonly selectedRowId?: string | null;
  readonly onSelectRow?: (rowId: string) => void;
  /** Estado del envío de la muestra de exposición de cada pase (BR-APP-84). */
  readonly exposureOf?: (passId: string) => 'pending' | ExposureOutcome | undefined;
}

export function PassMatrixTable({
  session,
  selectedRowId = null,
  onSelectRow,
  exposureOf,
}: PassMatrixTableProps) {
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <h2 id={headingId} className={styles.title}>
        {text('scr06.matrix.title')}
      </h2>
      <table className={styles.table} aria-labelledby={headingId}>
        <thead>
          <tr>
            <th scope="col">{text('scr06.matrix.row')}</th>
            <th scope="col">{text('scr06.matrix.posture')}</th>
            <th scope="col">{text('scr06.matrix.standard')}</th>
            <th scope="col">{text('scr06.matrix.conditions')}</th>
            <th scope="col">{text('scr06.matrix.required')}</th>
            <th scope="col">{text('scr06.matrix.progress')}</th>
            <th scope="col">{text('scr06.matrix.counts')}</th>
            {onSelectRow === undefined ? null : <th scope="col">{text('scr06.matrix.select')}</th>}
          </tr>
        </thead>
        <tbody>
          {session.rows.map((row, index) => {
            const { detected, missed, false_alarm: falseAlarm } = row.passes;
            const registered = detected + missed + falseAlarm;
            const posture = label('posture', row.posture);
            const number = index + 1;
            return (
              <tr
                key={row.row_id}
                data-part="matrix_row"
                data-row-id={row.row_id}
                data-selected={row.row_id === selectedRowId}
              >
                <th scope="row">{number}</th>
                <td data-part="posture">{posture}</td>
                <td data-part="standard_version">{row.standard_version}</td>
                <td data-part="conditions">{conditionsText(row.predicate_conditions)}</td>
                <td data-part="required_passes">{row.required_passes}</td>
                <td data-part="row_progress">
                  {`${String(registered)} / ${String(row.required_passes)}`}
                </td>
                <td data-part="row_counts">
                  {`${String(detected)} · ${String(missed)} · ${String(falseAlarm)}`}
                </td>
                {onSelectRow === undefined ? null : (
                  <td>
                    <button
                      type="button"
                      className={styles.button}
                      data-part="select_row"
                      data-row-id={row.row_id}
                      aria-label={`${text('scr06.matrix.select')}: ${posture} ${String(number)}`}
                      onClick={() => {
                        onSelectRow(row.row_id);
                      }}
                    >
                      {text('scr06.matrix.row')} {number}
                    </button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <h3 className={styles.subtitle}>{text('scr06.matrix.passes.title')}</h3>
      {session.passes.length === 0 ? (
        <p className={styles.empty} data-part="passes_empty">
          {text('scr06.matrix.passes.empty')}
        </p>
      ) : (
        <ul className={styles.passes} data-part="passes">
          {session.passes.map((pass) => {
            const outcome = exposureOf?.(pass.pass_id);
            return (
              <li key={pass.pass_id} data-part="pass" data-pass-id={pass.pass_id}>
                <span data-part="pass_result">{label('pass_result', pass.result)}</span>{' '}
                <span data-part="pass_recorded_at">
                  <ServerTimestamp instant={pass.recorded_at} />
                </span>{' '}
                <span data-part="pass_evidence">
                  {pass.evidence_ref ?? text('scr06.matrix.passes.evidence_none')}
                </span>
                {outcome === undefined ? null : (
                  <span data-part="exposure" data-outcome={outcome}>
                    {text(`scr06.matrix.exposure.${outcome}`)}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
