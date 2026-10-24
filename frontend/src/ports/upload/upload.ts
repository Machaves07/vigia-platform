/**
 * `ports.upload` (LC-APP-17, PAT-APP-SEG-02, BR-APP-48): subida directa al depósito.
 *
 * 1. Declaración: si la unidad la exige (adjuntos de cierre de U-04), sin la casilla «el adjunto
 *    no contiene ninguna persona identificable» no se pide nada a la red.
 * 2. Tipo y tamaño de la unidad dueña, comprobados antes de pedir la concesión (el servidor
 *    vuelve a comprobarlos y su respuesta manda).
 * 3. SHA-256 en el navegador con `crypto.subtle`.
 * 4. Concesión de la unidad dueña por `ports.base`: `POST /documents` (U-03) o
 *    `POST /loop/actions/{action_id}/attachments/upload-grant` (U-04, SUP-U05-09). Solo viajan
 *    los metadatos; el archivo nunca pasa por la API de la aplicación.
 * 5. `PUT` directo al origen concedido con las cabeceras de la concesión **tal cual**, incluida
 *    `x-amz-checksum-sha256`, y progreso. La URL prefirmada solo vive en esta llamada: no se
 *    devuelve, no se guarda y no se muestra.
 */
import type { components } from '@/client/schema';
import type { Clock } from '@/state/clock';
import type { Dependency } from '@/state/connection';

import { currentTransport, isPortError, PortError, type OperationId } from '../base/http';
import type { Behaviour } from '../errors/map';
import { validateBody, type BodyOperationId } from '../validation';

type DocumentKind = components['schemas']['DocumentKind'];
type DocumentGrantOut = components['schemas']['DocumentGrantOut'];
type UploadGrantResponse = components['schemas']['UploadGrantResponse'];

/** A quién se pide la concesión. */
export type UploadTarget =
  /** Documento firmado de planta (U-03, `POST /documents`): acta, acuerdo, política o captura. */
  | { readonly unit: 'catalog_document'; readonly plantId: string; readonly kind: DocumentKind }
  /** Adjunto de cierre de una acción del lazo (U-04, SUP-U05-09). */
  | { readonly unit: 'loop_attachment'; readonly actionId: string };

export type UploadUnit = UploadTarget['unit'];

export interface UploadRules {
  readonly contentTypes: readonly string[];
  readonly maxBytes: number;
  /** La unidad exige la declaración de ausencia de personas antes de la concesión. */
  readonly declarationRequired: boolean;
}

const MIB = 1024 * 1024;

/**
 * Tipos y tamaño de cada unidad dueña: U-03 (Infrastructure Design, `DOCUMENTS_MAX_BYTES` y
 * `DocumentContentType` del esquema) y U-04 (NFR Requirements: 10 MB, `MAX_ATTACHMENT_BYTES`;
 * el tope de 5 por entrada lo aplica la pantalla que arma la entrada).
 */
export const UPLOAD_RULES: Readonly<Record<UploadUnit, UploadRules>> = Object.freeze({
  catalog_document: Object.freeze({
    contentTypes: Object.freeze(['application/pdf', 'image/jpeg', 'image/png']),
    maxBytes: 20 * MIB,
    declarationRequired: false,
  }),
  loop_attachment: Object.freeze({
    contentTypes: Object.freeze(['image/jpeg', 'image/png', 'application/pdf']),
    maxBytes: 10 * MIB,
    declarationRequired: true,
  }),
});

/** Tope del `PUT` al depósito (BR-APP-61: ninguna petición sin tope) `[objetivo propio]`. */
const UPLOAD_PUT_TIMEOUT_MS = 10 * 60 * 1000;

const CHECKSUM_HEADER = 'x-amz-checksum-sha256';

/** Códigos de rechazo del adjunto que se anclan al campo (BR-APP-48, nota de §13). */
const ATTACHMENT_ERROR_CODES = [
  'attachment_type_not_allowed',
  'attachment_too_large',
  'attachment_hash_mismatch',
  'attachment_declaration_missing',
  'attachment_metadata_not_allowed',
] as const;
export type AttachmentErrorCode = (typeof ATTACHMENT_ERROR_CODES)[number];

/** `attachment_*` desde el `detail_code` de U-04 (`loop_attachment_*`); si no, `null`. */
export function attachmentErrorCode(
  detailCode: string | null | undefined,
): AttachmentErrorCode | null {
  if (typeof detailCode !== 'string' || !detailCode.startsWith('loop_')) return null;
  const code = detailCode.slice('loop_'.length);
  return (ATTACHMENT_ERROR_CODES as readonly string[]).includes(code)
    ? (code as AttachmentErrorCode)
    : null;
}

/** Por qué falló una subida; el archivo elegido sigue en manos del llamador. */
export type UploadFailure =
  /** Rechazo del adjunto, local o de la unidad (`detail_code`). */
  | { readonly kind: 'attachment'; readonly code: AttachmentErrorCode }
  /** El archivo está vacío: no hay nada que subir. */
  | { readonly kind: 'empty' }
  /** El `PUT` al depósito falló, venció o el origen no es válido; se puede reintentar. */
  | { readonly kind: 'store' }
  /** La concesión falló en la API: el comportamiento sale de `behaviourFor` (BR-APP-71). */
  | { readonly kind: 'port'; readonly error: PortError; readonly behaviour: Behaviour | null };

class UploadError extends Error {
  readonly failure: UploadFailure;

  constructor(failure: UploadFailure) {
    super(`ports.upload: ${failure.kind}`);
    this.name = 'UploadError';
    this.failure = failure;
  }
}

export function isUploadError(value: unknown): value is UploadError {
  return value instanceof UploadError;
}

/** Lo que la pantalla necesita para registrar el adjunto o el documento (sin URL). */
export type UploadResult =
  | {
      readonly unit: 'catalog_document';
      readonly document_ref: components['schemas']['DocumentRefOut'];
    }
  | {
      readonly unit: 'loop_attachment';
      /** Forma de `EvidenceAttachmentBody` de `POST /loop/actions/{action_id}/evidence`. */
      readonly attachment: {
        readonly attachment_id: string;
        readonly content_type: string;
        readonly size_bytes: number;
        readonly sha256: string;
        readonly filename: string;
        readonly no_identifiable_person_declared: true;
      };
      readonly storage_key: string;
    };

interface UploadProgress {
  readonly loaded: number;
  readonly total: number;
}

/** El `PUT` de la concesión; inyectable en las pruebas. */
export type PutObject = (request: {
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly file: Blob;
  readonly clock: Clock;
  readonly onProgress: (progress: UploadProgress) => void;
}) => Promise<void>;

type UploadStage = 'hashing' | 'granting' | 'uploading';

export interface UploadOptions {
  /** La persona marcó «el adjunto no contiene ninguna persona identificable». */
  readonly declared: boolean;
  readonly onStage?: (stage: UploadStage) => void;
  readonly onProgress?: (progress: UploadProgress) => void;
  readonly putObject?: PutObject;
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function base64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes));
}

/** SHA-256 del archivo con `crypto.subtle`: hexadecimal (concesión) y base64 (cabecera). */
async function digestFile(file: Blob): Promise<{ hex: string; base64: string }> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
  return { hex: hex(digest), base64: base64(digest) };
}

/** Comprobaciones locales (declaración, vacío, tipo y tamaño); nada sale a la red si fallan. */
function precheck(target: UploadTarget, file: Blob, declared: boolean): UploadFailure | null {
  const rules = UPLOAD_RULES[target.unit];
  if (rules.declarationRequired && !declared) {
    return { kind: 'attachment', code: 'attachment_declaration_missing' };
  }
  if (!rules.contentTypes.includes(file.type)) {
    return { kind: 'attachment', code: 'attachment_type_not_allowed' };
  }
  if (file.size === 0) return { kind: 'empty' };
  if (file.size > rules.maxBytes) return { kind: 'attachment', code: 'attachment_too_large' };
  return null;
}

/** Concesión por `ports.base` con el cuerpo validado contra su espejo (BR-APP-64). */
async function grant(
  operationId: OperationId & BodyOperationId,
  dependency: Dependency,
  path: Readonly<Record<string, string>> | undefined,
  body: unknown,
): Promise<unknown> {
  const verdict = validateBody(operationId, body);
  if (!verdict.ok) throw new PortError({ kind: 'invalid_input', issues: verdict.issues });
  return currentTransport().request({
    operationId,
    kind: 'write',
    dependency,
    ...(path === undefined ? {} : { path }),
    body,
  });
}

function portFailure(error: unknown): UploadFailure {
  if (!isPortError(error)) throw error;
  const { failure } = error;
  if (failure.kind === 'http') {
    const code = attachmentErrorCode(failure.error.detail_code);
    if (code !== null) return { kind: 'attachment', code };
  }
  const behaviour =
    failure.kind === 'http' || failure.kind === 'timeout' || failure.kind === 'network'
      ? failure.behaviour
      : null;
  return { kind: 'port', error, behaviour };
}

/** Solo `https:` (o `http:` si la propia aplicación se sirve así, en desarrollo). */
function grantedUrl(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const page = globalThis.location.protocol;
  if (parsed.protocol === 'https:' || (parsed.protocol === 'http:' && page === 'http:')) {
    return parsed.href;
  }
  return null;
}

/** `PUT` con `XMLHttpRequest`: el único medio del navegador con progreso de subida. */
const xhrPutObject: PutObject = ({ url, headers, file, clock, onProgress }) =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const timer = clock.setTimeout(() => {
      xhr.abort();
    }, UPLOAD_PUT_TIMEOUT_MS);
    const settle = (ok: boolean) => {
      clock.clearTimeout(timer);
      if (ok) resolve();
      else reject(new UploadError({ kind: 'store' }));
    };
    xhr.open('PUT', url);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      onProgress({ loaded: event.loaded, total: event.lengthComputable ? event.total : file.size });
    };
    xhr.onload = () => {
      settle(xhr.status >= 200 && xhr.status < 300);
    };
    xhr.onerror = () => {
      settle(false);
    };
    xhr.onabort = () => {
      settle(false);
    };
    xhr.send(file);
  });

interface Granted {
  readonly url: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly result: UploadResult;
}

async function requestGrant(
  target: UploadTarget,
  file: File,
  digest: { hex: string },
): Promise<Granted> {
  if (target.unit === 'catalog_document') {
    const out = (await grant('grant_document_documents_post', 'document_store', undefined, {
      plant_id: target.plantId,
      kind: target.kind,
      content_type: file.type,
      size_bytes: file.size,
      sha256: digest.hex,
    })) as DocumentGrantOut;
    return {
      url: out.upload.url,
      headers: out.upload.required_headers,
      result: { unit: 'catalog_document', document_ref: out.document_ref },
    };
  }
  const out = (await grant(
    'upload_grant_loop_actions__action_id__attachments_upload_grant_post',
    'evidence_store',
    { action_id: target.actionId },
    { content_type: file.type, size_bytes: file.size, sha256: digest.hex },
  )) as UploadGrantResponse;
  return {
    url: out.upload.url,
    headers: out.upload.required_headers,
    result: {
      unit: 'loop_attachment',
      attachment: {
        attachment_id: out.attachment_id,
        content_type: file.type,
        size_bytes: file.size,
        sha256: digest.hex,
        filename: file.name.slice(0, 128) || 'adjunto',
        no_identifiable_person_declared: true,
      },
      storage_key: out.storage_key,
    },
  };
}

/** La cabecera de suma de la concesión debe ser la del archivo; si falta, se pone la propia. */
function putHeaders(
  granted: Readonly<Record<string, string>>,
  checksum: string,
): Readonly<Record<string, string>> | null {
  const headers: Record<string, string> = {};
  let found = false;
  for (const [name, value] of Object.entries(granted)) {
    if (name.toLowerCase() === CHECKSUM_HEADER) {
      if (value !== checksum) return null;
      found = true;
    }
    headers[name] = value;
  }
  if (!found) headers[CHECKSUM_HEADER] = checksum;
  return headers;
}

/** Sube `file` a la unidad de `target`; lanza `UploadError` y nunca devuelve la URL. */
export async function uploadFile(
  target: UploadTarget,
  file: File,
  options: UploadOptions,
): Promise<UploadResult> {
  const rejected = precheck(target, file, options.declared);
  if (rejected !== null) throw new UploadError(rejected);
  options.onStage?.('hashing');
  const digest = await digestFile(file);
  let granted: Granted;
  try {
    options.onStage?.('granting');
    granted = await requestGrant(target, file, digest);
  } catch (error) {
    throw new UploadError(portFailure(error));
  }
  const url = grantedUrl(granted.url);
  const headers = putHeaders(granted.headers, digest.base64);
  if (url === null) throw new UploadError({ kind: 'store' });
  if (headers === null) {
    throw new UploadError({ kind: 'attachment', code: 'attachment_hash_mismatch' });
  }
  const put = options.putObject ?? xhrPutObject;
  options.onStage?.('uploading');
  try {
    await put({
      url,
      headers,
      file,
      clock: currentTransport().clock,
      onProgress: options.onProgress ?? (() => undefined),
    });
  } catch {
    throw new UploadError({ kind: 'store' });
  }
  return granted.result;
}
