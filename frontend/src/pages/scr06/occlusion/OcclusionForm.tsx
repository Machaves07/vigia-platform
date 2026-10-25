/**
 * Formulario de la prueba de oclusión de una cámara (IF-13 paso 5): la cámara ocluida, el inicio y
 * el fin de la ventana y, opcionalmente, el resultado declarado con su motivo
 * (`declared_reason_es`). Las marcas se escriben en ISO 8601 con zona o se toman del reloj con
 * «marcar ahora»; el servidor comprueba que el fin no pase de su propia hora.
 *
 * La pantalla solo comprueba lo estructural (BR-APP-42): el espejo del esquema valida la forma de
 * cada campo y aquí se añade que el fin sea posterior al inicio. La política de texto libre del
 * motivo (10 a 500 caracteres) la aplica el servidor y su aviso llega anclado al campo.
 */
import { useState } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { OptionalTextArea } from '@/pages/scr06/common/OptionalTextArea';
import { TextField } from '@/pages/scr06/common/TextField';
import type { FieldIssues } from '@/pages/scr06/common/fieldIssues';
import type { OcclusionTestBody } from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import styles from './occlusion.module.css';

/** Valores del formulario (texto tal como se escribe). */
export interface OcclusionDraft {
  readonly camera_id: string;
  readonly started_at: string;
  readonly ended_at: string;
  readonly declared_reason_es: string;
}

export const EMPTY_OCCLUSION_DRAFT: OcclusionDraft = {
  camera_id: '',
  started_at: '',
  ended_at: '',
  declared_reason_es: '',
};

/** El cuerpo exacto del contrato: sin motivo, el campo no viaja. */
function occlusionBody(draft: OcclusionDraft): OcclusionTestBody {
  const reason = draft.declared_reason_es.trim();
  return {
    camera_id: draft.camera_id.trim(),
    started_at: draft.started_at.trim(),
    ended_at: draft.ended_at.trim(),
    ...(reason === '' ? {} : { declared_reason_es: draft.declared_reason_es }),
  };
}

/** `true` si las dos marcas se leen y el fin es posterior al inicio. */
function windowInOrder(draft: OcclusionDraft): boolean {
  const start = Date.parse(draft.started_at.trim());
  const end = Date.parse(draft.ended_at.trim());
  return Number.isNaN(start) || Number.isNaN(end) || end > start;
}

export interface OcclusionFormProps {
  readonly draft: OcclusionDraft;
  readonly onChange: (draft: OcclusionDraft) => void;
  readonly onSubmit: (body: OcclusionTestBody) => void;
  readonly clock: Clock;
  readonly busy: boolean;
  /** Problemas estructurales del espejo del esquema, por campo. */
  readonly issues: FieldIssues;
  /** Aviso del servidor anclado a un campo del formulario. */
  readonly noticeFor: (field: string) => Notice | undefined;
}

export function OcclusionForm({
  draft,
  onChange,
  onSubmit,
  clock,
  busy,
  issues,
  noticeFor,
}: OcclusionFormProps) {
  const [orderError, setOrderError] = useState(false);
  const set = (field: keyof OcclusionDraft) => (value: string) => {
    setOrderError(false);
    onChange({ ...draft, [field]: value });
  };
  const now = () => new Date(clock.now()).toISOString();
  // Un rechazo sin motivo declarado (cámara ajena, ventana fuera de rango) se ancla a la cámara.
  const cameraNotice = noticeFor('camera_id');
  return (
    <section className={styles.panel} aria-label={text('scr06.occlusion.form.title')}>
      <h2 className={styles.title}>{text('scr06.occlusion.form.title')}</h2>
      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (!windowInOrder(draft)) {
            setOrderError(true);
            return;
          }
          onSubmit(occlusionBody(draft));
        }}
      >
        <TextField
          label={text('scr06.occlusion.form.camera')}
          hint={text('scr06.occlusion.form.camera_hint')}
          value={draft.camera_id}
          onChange={set('camera_id')}
          errorKey={issues.get('camera_id') ?? null}
          required
        />
        {cameraNotice === undefined ? null : (
          <FieldNotice id={`occlusion-camera-${cameraNotice.id}`} notice={cameraNotice} />
        )}
        <div className={styles.window}>
          <TextField
            label={text('scr06.occlusion.form.started_at')}
            hint={text('scr06.occlusion.form.window_hint')}
            value={draft.started_at}
            onChange={set('started_at')}
            errorKey={issues.get('started_at') ?? null}
            required
          />
          <button
            type="button"
            className={styles.secondary}
            onClick={() => {
              set('started_at')(now());
            }}
          >
            {text('scr06.occlusion.form.mark_start')}
          </button>
          <TextField
            label={text('scr06.occlusion.form.ended_at')}
            value={draft.ended_at}
            onChange={set('ended_at')}
            errorKey={
              issues.get('ended_at') ?? (orderError ? 'scr06.occlusion.form.window_order' : null)
            }
            required
          />
          <button
            type="button"
            className={styles.secondary}
            onClick={() => {
              set('ended_at')(now());
            }}
          >
            {text('scr06.occlusion.form.mark_end')}
          </button>
        </div>
        <OptionalTextArea
          label={text('scr06.occlusion.form.declared_reason')}
          hint={text('scr06.occlusion.form.declared_hint')}
          value={draft.declared_reason_es}
          onChange={set('declared_reason_es')}
          notice={noticeFor('declared_reason_es')}
          errorKey={issues.get('declared_reason_es') ?? null}
        />
        <div className={styles.actions}>
          <button
            type="submit"
            className={styles.button}
            disabled={busy}
            data-part="record_occlusion"
          >
            {text(busy ? 'scr06.occlusion.form.submitting' : 'scr06.occlusion.form.submit')}
          </button>
        </div>
      </form>
    </section>
  );
}
