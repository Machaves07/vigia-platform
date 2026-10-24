/**
 * `Notice` (domain-entities §2.11): un error recibido del servidor y dónde se presenta. El
 * comportamiento sale siempre del mapa cerrado `behaviourFor` (BR-APP-71); aquí solo se decide
 * si va anclado a un campo del formulario o a la pila global.
 */
import { behaviourFor, type Behaviour, type HttpError } from '@/ports/errors/map';

export interface Notice {
  /** Identificador local del aviso (no del servidor); un aviso nuevo con el mismo id lo sustituye. */
  readonly id: string;
  readonly error: HttpError;
  /** `correlation_id` del cuerpo de error; si falta, se lee de `error.body`. Nunca del usuario. */
  readonly correlation_id?: string | null;
  /** Campo del formulario al que la pantalla ancla el aviso; sin campo, el aviso es global. */
  readonly field?: string | null;
}

/** `correlation_id` del contrato (`format: uuid`); cualquier otra cosa no se muestra. */
const CORRELATION = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function correlationOf(notice: Notice): string | null {
  const body = notice.error.body;
  const fromBody =
    typeof body === 'object' &&
    body !== null &&
    !Array.isArray(body) &&
    Object.hasOwn(body, 'correlation_id')
      ? (body as Record<string, unknown>).correlation_id
      : undefined;
  const value = notice.correlation_id ?? fromBody;
  return typeof value === 'string' && CORRELATION.test(value) ? value : null;
}

/** Solo `invalid_request` y `conflict` (comportamiento `anchored`) se anclan a un campo (BR-APP-78). */
export function isAnchored(notice: Notice, behaviour: Behaviour = behaviourFor(notice.error)) {
  return behaviour.kind === 'anchored' && typeof notice.field === 'string' && notice.field !== '';
}

/** El aviso anclado a `field`, si lo hay (el último recibido manda). */
export function noticeForField(notices: readonly Notice[], field: string): Notice | undefined {
  return notices.findLast((notice) => isAnchored(notice) && notice.field === field);
}
