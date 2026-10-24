/**
 * `ports.upload` (LC-APP-17): subida directa con hash, declaración, concesión de la unidad dueña
 * y `PUT` con suma (`upload.ts`), y las URL prefirmadas de lectura de clips y adjuntos de cierre
 * (`readUrls.ts`). Lo usan los componentes de medios (UI-12, UI-21); una página no lo importa
 * (BR-APP-62): pasa el destino al componente.
 */
export {
  attachmentErrorCode,
  isUploadError,
  UPLOAD_RULES,
  uploadFile,
  type PutObject,
  type UploadFailure,
  type UploadResult,
  type UploadTarget,
} from './upload';
export { readClosureAttachmentUrl, readEvidenceUrl, type ReadUrl } from './readUrls';
