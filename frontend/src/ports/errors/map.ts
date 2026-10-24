/** Mapa total BR-APP-71, LC-APP-15. Solo describe acciones: no reintenta ni altera el circuito. */
import texts from '../../texts/errors.json';

import { detailLabel, exportStateLabel } from './labels';

export interface HttpError {
  readonly code: string;
  readonly status: number;
  readonly detail_code?: string | null;
  readonly retry_after_seconds?: number | null;
  readonly message_es?: string;
  readonly body?: unknown;
}

interface Message {
  readonly text_key: keyof typeof texts;
  readonly message_es: string;
}
type Section =
  | 'storage'
  | 'gate_state'
  | 'standard_text'
  | 'coverage'
  | 'unclosable_policy'
  | 'assistant'
  | 'business';

export type Behaviour = Message &
  (
    | { readonly kind: 'reauth'; readonly preserve_draft: true; readonly preserve_route: true }
    | {
        readonly kind:
          | 'second_factor'
          | 'privacy_notice'
          | 'not_available_in_scope'
          | 'forbidden_reload'
          | 'period_too_long'
          | 'zone_without_node'
          | 'internal';
      }
    | {
        readonly kind: 'anchored';
        readonly preserve_draft: true;
        readonly export_state?: string;
        readonly export_state_label?: string;
      }
    | { readonly kind: 'payload_too_large'; readonly preserve_draft: true }
    | {
        readonly kind: 'countdown' | 'transient';
        readonly retry_after_seconds: number | null;
      }
    | { readonly kind: 'degraded_section'; readonly section: Section }
  );

const unavailableSections: Readonly<Record<string, Section>> = Object.freeze({
  loop_gate_state_unavailable: 'gate_state',
  loop_standard_text_unavailable: 'standard_text',
  accreditation_coverage_unavailable: 'coverage',
  loop_unclosable_policy_unavailable: 'unclosable_policy',
  loop_assistant_unavailable: 'assistant',
});

function message(text_key: keyof typeof texts): Message {
  return { text_key, message_es: texts[text_key] };
}

function bodyField(body: unknown, key: string): unknown {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return undefined;
  return Object.hasOwn(body, key) ? (body as Record<string, unknown>)[key] : undefined;
}

/** Se acepta solo el mensaje público del contrato, nunca trazas ni el cuerpo convertido a texto. */
function serverMessage(error: HttpError): string | undefined {
  const value = error.message_es ?? bodyField(error.body, 'message_es');
  return typeof value === 'string' && value.trim().length > 0 && value.length <= 256
    ? value
    : undefined;
}

function businessDetail(value: string | null | undefined): value is string {
  // app_ sigue reservado, sin etiquetas propias; permite la lectura tolerante de un valor futuro.
  return (
    typeof value === 'string' &&
    value.length <= 64 &&
    /^(?:catalog|fleet|loop|accreditation|notifications|app)_[a-z0-9]+(?:_[a-z0-9]+)*$/.exec(
      value,
    )?.[0] === value
  );
}

function businessMessage(error: HttpError, fallback: keyof typeof texts): Message {
  const label = businessDetail(error.detail_code) ? detailLabel(error.detail_code) : undefined;
  return { text_key: fallback, message_es: label ?? serverMessage(error) ?? texts[fallback] };
}

function retryAfter(error: HttpError): number | null {
  const value = error.retry_after_seconds;
  // Un retardo ausente o inválido no inventa una espera ni permite un temporizador infinito.
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 3600
    ? value
    : null;
}

/** code manda sobre detail_code: un detalle nunca elude autenticación ni revela otro alcance. */
export function behaviourFor(error: HttpError): Behaviour {
  switch (error.code) {
    case 'unauthenticated':
      return {
        kind: 'reauth',
        ...message('errors.reauth'),
        preserve_draft: true,
        preserve_route: true,
      };
    case 'second_factor_required':
      return { kind: 'second_factor', ...message('errors.second_factor') };
    case 'privacy_notice_required':
      return { kind: 'privacy_notice', ...message('errors.privacy_notice') };
    case 'forbidden':
      return { kind: 'forbidden_reload', ...message('errors.forbidden_reload') };
    case 'not_found':
      return { kind: 'not_available_in_scope', ...message('errors.not_available_in_scope') };
    case 'invalid_request':
    case 'conflict': {
      const state = bodyField(error.body, 'export_state');
      const label = typeof state === 'string' ? exportStateLabel(state) : undefined;
      if (
        error.code === 'conflict' &&
        error.status === 409 &&
        !error.detail_code &&
        label !== undefined &&
        typeof state === 'string'
      ) {
        return {
          kind: 'anchored',
          ...message('errors.export_state'),
          preserve_draft: true,
          export_state: state,
          export_state_label: label,
        };
      }
      return {
        kind: 'anchored',
        ...businessMessage(error, 'errors.anchored'),
        preserve_draft: true,
      };
    }
    case 'payload_too_large':
      return {
        kind: 'payload_too_large',
        ...message('errors.payload_too_large'),
        preserve_draft: true,
      };
    case 'throttled':
    case 'rate_limited':
      return {
        kind: 'countdown',
        ...message('errors.countdown'),
        retry_after_seconds: retryAfter(error),
      };
    case 'temporarily_unavailable':
      if (businessDetail(error.detail_code)) {
        return {
          kind: 'degraded_section',
          ...businessMessage(error, 'errors.degraded_section'),
          section: unavailableSections[error.detail_code] ?? 'business',
        };
      }
      return {
        kind: 'transient',
        ...message('errors.transient'),
        retry_after_seconds: retryAfter(error),
      };
    case 'storage_unavailable':
      return {
        kind: 'degraded_section',
        ...message('errors.storage_unavailable'),
        section: 'storage',
      };
    case 'period_too_long':
      return { kind: 'period_too_long', ...message('errors.period_too_long') };
    case 'zone_without_node':
      return { kind: 'zone_without_node', ...message('errors.zone_without_node') };
    case 'internal_error':
    default:
      return { kind: 'internal', ...message('errors.internal_error') };
  }
}

/** El transporte se normaliza en ports.base; cancelaciones del usuario no son timeout ni network. */
export function counts_for_circuit(
  error: HttpError | { readonly kind: 'timeout' | 'network' },
): boolean {
  if ('kind' in error) return true;
  return error.code === 'temporarily_unavailable' && !businessDetail(error.detail_code);
}
