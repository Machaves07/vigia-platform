/**
 * Reejecución por regresión (IF-13 paso 7; interfaces U-03 §3.3 y v1.5): con la regresión de la
 * zona pendiente, `POST /zones/{zone_id}/walk-tests/regression-rerun` abre una sesión
 * `regression_rerun` con **solo las filas afectadas**, que decide el servidor. La pantalla no
 * consulta antes si hay regresión ni oculta la acción (BR-APP-43): si no la hay, el servidor
 * responde `conflict` y el aviso se muestra; la zona sigue productiva.
 */
import { useState } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { TextField } from '@/pages/scr06/common/TextField';
import { MIN_PASSES_PER_CELL } from '@/ports/scr06Commissioning';
import { text } from '@/texts';

import styles from './session.module.css';

export function RegressionRerunPanel({
  onSubmit,
  busy,
  issue,
  notice,
}: {
  readonly onSubmit: (passesPerCell: number) => void;
  readonly busy: boolean;
  /** Error estructural del espejo del esquema para `passes_per_cell`. */
  readonly issue: string | null;
  /** Aviso del servidor anclado al formulario de la reejecución. */
  readonly notice: Notice | undefined;
}) {
  const [value, setValue] = useState(String(MIN_PASSES_PER_CELL));
  const [showErrors, setShowErrors] = useState(false);
  const parsed = /^\d+$/.test(value.trim()) ? Number.parseInt(value.trim(), 10) : null;
  const errorKey = issue ?? (showErrors && parsed === null ? 'validation.out_of_range' : null);
  return (
    <section className={styles.panel} aria-label={text('scr06.rerun.title')}>
      <h2 className={styles.title}>{text('scr06.rerun.title')}</h2>
      <p className={styles.empty}>{text('scr06.rerun.hint')}</p>
      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (parsed === null) {
            setShowErrors(true);
            return;
          }
          onSubmit(parsed);
        }}
      >
        <TextField
          label={text('scr06.rerun.passes_per_cell')}
          value={value}
          onChange={setValue}
          type="number"
          min={MIN_PASSES_PER_CELL}
          errorKey={errorKey}
          required
        />
        {notice === undefined ? null : <FieldNotice id={`rerun-${notice.id}`} notice={notice} />}
        <div className={styles.actions}>
          <button type="submit" className={styles.button} disabled={busy} data-part="open_rerun">
            {text(busy ? 'scr06.rerun.submitting' : 'scr06.rerun.submit')}
          </button>
        </div>
      </form>
    </section>
  );
}
