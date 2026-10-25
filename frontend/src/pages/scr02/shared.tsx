/**
 * Piezas comunes de los dos paneles de SCR-02 (VIG-322): fechas en la zona del navegador (una
 * concesión es de la organización, no de una planta), avisos de las escrituras y etiquetas del
 * estado y de quién revocó.
 *
 * Un `invalid_request` o `conflict` (comportamiento `anchored`) se ancla al campo que nombre la
 * pantalla con el mensaje del servidor (`FieldNotice`) y no toca lo escrito; otra respuesta del
 * servidor muestra el aviso genérico; un fallo local, el suyo.
 */
import type { ReactNode } from 'react';

import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import { FieldNotice } from '@/components/NoticeStack/NoticeStack';
import type { Notice } from '@/components/NoticeStack/notice';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { localFailure } from '@/ports/scr02Concessions';
import { text } from '@/texts';
import { label } from '@/texts/labels';

/** Zona horaria del navegador para concedida, vence y momento. */
export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

export function Moment({
  instant,
  timeZone,
}: {
  readonly instant: string;
  readonly timeZone: string;
}): ReactNode {
  return <TimestampWithOffset instant={instant} timeZone={timeZone} />;
}

export function statusLabel(status: string): string {
  return label('concession_status', status);
}

/** Quién revocó (`revoked_by_side`), o «no revocada». */
export function revokedByText(side: string | null): string {
  if (side === 'client') return text('scr02.revoked_by.client');
  if (side === 'provider') return text('scr02.revoked_by.provider');
  return side === null ? text('scr02.revoked_by.none') : text('common.unknown');
}

/** Filas sin paginación con las mismas celdas que la tabla, para las vistas registradas. */
export function StaticRows<Row>({
  caption,
  columns,
  rows,
}: {
  readonly caption: string;
  readonly columns: readonly KeysetColumn<Row>[];
  readonly rows: readonly Row[];
}): ReactNode {
  return (
    <table>
      <caption>{caption}</caption>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.id} scope="col">
              {column.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index}>
            {columns.map((column) => (
              <td key={column.id}>{column.cell(row)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Avisos de una escritura no anclados a un campo: los globales y los fallos locales. */
export function WriteNotices({
  notice,
  error,
  anchoredId,
}: {
  readonly notice: Notice | null;
  readonly error: Error | null;
  /** Identificador del aviso anclado al formulario, si este lo pinta aquí. */
  readonly anchoredId?: string;
}): ReactNode {
  const local = localFailure(error);
  return (
    <>
      {notice?.field === 'form' && anchoredId !== undefined && (
        <FieldNotice id={anchoredId} notice={notice} />
      )}
      {notice !== null && (notice.field ?? null) === null && (
        <p role="alert" data-code={notice.error.code}>
          {text('screen_state.error')}
        </p>
      )}
      {local === 'invalid_input' && <p role="alert">{text('scr02.request.invalid')}</p>}
      {local === 'unavailable' && <p role="alert">{text('screen_state.unavailable')}</p>}
    </>
  );
}
