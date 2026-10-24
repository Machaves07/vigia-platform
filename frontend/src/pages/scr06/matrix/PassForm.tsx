/**
 * Registro de un pase (IF-13 paso 3, U05-H-11): la fila elegida, el resultado de la lista cerrada
 * (`detected`, `missed`, `false_alarm`) y, opcionalmente, la **evidencia**: un `clip_id` de los
 * clips de comisionamiento de la zona, elegido en el selector. Nunca se sube un archivo: la
 * consola no tiene campo de archivo y el cuerpo solo lleva `evidence_ref`.
 */
import { useId, useState } from 'react';

import type { CommissioningClip, PassResult, MatrixRow } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './matrix.module.css';

/** Resultados admitidos de un pase, en el orden de la lista cerrada de U-03. */
const PASS_RESULTS: readonly PassResult[] = ['detected', 'missed', 'false_alarm'];

export interface PassInput {
  readonly row_id: string;
  readonly result: PassResult;
  readonly evidence_ref: string | null;
}

export interface PassFormProps {
  /** Fila elegida en la matriz; sin fila no se envía nada. */
  readonly rowId: string | null;
  readonly rows: readonly MatrixRow[];
  readonly clips: readonly CommissioningClip[];
  readonly onSubmit: (input: PassInput) => void;
  readonly busy: boolean;
  /** Error estructural del espejo del esquema, por campo. */
  readonly errorKey?: string | null;
}

export function PassForm({ rowId, rows, clips, onSubmit, busy, errorKey = null }: PassFormProps) {
  const [result, setResult] = useState<PassResult>('detected');
  const [evidence, setEvidence] = useState('');
  const resultId = useId();
  const evidenceId = useId();
  const hintId = `${evidenceId}-hint`;
  if (rowId === null) {
    return (
      <p className={styles.empty} data-part="choose_row">
        {text('scr06.matrix.form.choose_row')}
      </p>
    );
  }
  const row = rows.find((candidate) => candidate.row_id === rowId);
  return (
    <form
      className={styles.form}
      aria-label={text('scr06.matrix.form.title')}
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit({
          row_id: rowId,
          result,
          evidence_ref: evidence === '' ? null : evidence,
        });
      }}
    >
      <h3 className={styles.subtitle}>{text('scr06.matrix.form.title')}</h3>
      <p className={styles.hint} data-part="selected_row">
        {`${text('scr06.matrix.selected')}: ${
          row === undefined ? rowId : label('posture', row.posture)
        }`}
      </p>
      <div className={styles.field}>
        <label htmlFor={resultId} className={styles.label}>
          {text('scr06.matrix.form.result')}
        </label>
        <select
          id={resultId}
          className={styles.control}
          data-part="result_select"
          value={result}
          onChange={(event) => {
            setResult(event.target.value as PassResult);
          }}
        >
          {PASS_RESULTS.map((value) => (
            <option key={value} value={value}>
              {label('pass_result', value)}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor={evidenceId} className={styles.label}>
          {text('scr06.matrix.form.evidence')}
        </label>
        <p id={hintId} className={styles.hint}>
          {text('scr06.matrix.form.evidence_hint')}
        </p>
        <select
          id={evidenceId}
          className={styles.control}
          data-part="evidence_select"
          aria-describedby={hintId}
          aria-invalid={errorKey !== null}
          value={evidence}
          onChange={(event) => {
            setEvidence(event.target.value);
          }}
        >
          <option value="">{text('scr06.matrix.form.evidence_none')}</option>
          {clips.map((clip) => (
            <option key={clip.clip_id} value={clip.clip_id}>
              {clip.clip_id}
            </option>
          ))}
        </select>
        {errorKey === null ? null : (
          <p className={styles.error} data-part="local_error" data-text-key={errorKey}>
            {text(errorKey)}
          </p>
        )}
      </div>
      <div className={styles.actions}>
        <button type="submit" className={styles.button} disabled={busy} data-part="register_pass">
          {text(busy ? 'scr06.matrix.form.submitting' : 'scr06.matrix.form.submit')}
        </button>
      </div>
    </form>
  );
}
