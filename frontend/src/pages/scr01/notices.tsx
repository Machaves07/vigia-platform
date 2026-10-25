/**
 * Avisos de las escrituras de cuenta y usuarios (VIG-305; BR-APP-71 a 78): un `invalid_request`
 * o `conflict` (comportamiento `anchored`) se ancla al formulario con el mensaje del servidor
 * (`FieldNotice`, que el formulario referencia con `aria-describedby`) y no toca lo escrito; otra
 * respuesta del servidor muestra el aviso genérico; un fallo local, el suyo.
 */
import type { ReactNode } from 'react';

import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import { localFailure, writeNotice } from '@/ports/scr01Admin';
import { text } from '@/texts';

/** Identificador del aviso anclado, para `aria-describedby`, o `undefined` si no lo hay. */
export function anchoredNoticeId(error: Error | null, formId: string): string | undefined {
  return writeNotice(error, formId)?.field === 'form' ? `${formId}-notice` : undefined;
}

export function WriteNotices({
  error,
  formId,
}: {
  readonly error: Error | null;
  readonly formId: string;
}): ReactNode {
  const notice = writeNotice(error, formId);
  const local = localFailure(error);
  return (
    <>
      {notice?.field === 'form' && <FieldNotice id={`${formId}-notice`} notice={notice} />}
      {notice !== null && notice.field !== 'form' && (
        <p role="alert" data-code={notice.error.code}>
          {text('screen_state.error')}
        </p>
      )}
      {local === 'invalid_input' && <p role="alert">{text('scr01_admin.invalid')}</p>}
      {local === 'unavailable' && <p role="alert">{text('screen_state.unavailable')}</p>}
    </>
  );
}
