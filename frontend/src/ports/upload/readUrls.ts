/**
 * URL prefirmadas de lectura (BR-APP-58, SUP-U05-10): se piden por `ports.base` en el momento en
 * que se van a usar y se devuelven al llamador, que las usa una vez y las suelta. Nunca pasan por
 * `state.query` ni por almacenamiento alguno.
 *
 * - `POST /evidence/{evidence_id}/read-url`: clip difuminado, vigente 5 minutos (UI-12).
 * - `POST /loop/attachments/{attachment_id}/read-url`: adjunto de cierre, vigente 15 minutos;
 *   se abre por navegación en pestaña nueva, nunca incrustado (nota U05-H-12).
 *
 * Ambas cuentan en el circuito `evidence_store` (PAT-APP-RES-01): un `storage_unavailable` degrada
 * el medio y nunca la plataforma.
 */
import { currentTransport } from '../base/http';

/** Una URL de lectura y su vencimiento (ISO 8601 del servidor). */
export interface ReadUrl {
  readonly url: string;
  readonly expires_at: string;
}

function readUrl(body: unknown): ReadUrl {
  const value = body as Partial<Record<'url' | 'expires_at', unknown>> | null;
  if (typeof value?.url !== 'string' || typeof value.expires_at !== 'string') {
    throw new TypeError('ports.upload: respuesta de URL de lectura sin url o expires_at.');
  }
  return { url: value.url, expires_at: value.expires_at };
}

/** URL de un clip (`POST` auditado: una escritura sin reintentos, BR-APP-72). */
export async function readEvidenceUrl(evidenceId: string): Promise<ReadUrl> {
  return readUrl(
    await currentTransport().request({
      operationId: 'read_url_evidence__evidence_id__read_url_post',
      kind: 'write',
      path: { evidence_id: evidenceId },
      dependency: 'evidence_store',
    }),
  );
}

/** URL de descarga de un adjunto de cierre (`closure_attachments.read`). */
export async function readClosureAttachmentUrl(attachmentId: string): Promise<ReadUrl> {
  return readUrl(
    await currentTransport().request({
      operationId: 'read_url_loop_attachments__attachment_id__read_url_post',
      kind: 'write',
      path: { attachment_id: attachmentId },
      dependency: 'evidence_store',
    }),
  );
}
