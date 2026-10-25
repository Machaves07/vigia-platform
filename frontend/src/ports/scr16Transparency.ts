/**
 * Puerto de SCR-16, vista de transparencia (IF-19, H-55): lo que la representación de los
 * trabajadores ve de su zona. Solo la vista de transparencia y la confirmación de la propia firma
 * (BR-APP-52, con `transparency.read` y sin ningún permiso de gestión); la vista en vivo la pide
 * `LiveViewLauncher` con su puerto (`ports.liveview`, `live_view.open`).
 */
import type { components } from '@/client/schema';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type Transparency = components['schemas']['TransparencyOut'];
export type TransparencyAgreement = components['schemas']['AgreementOut'];
export type TransparencyGates = components['schemas']['GatesOut'];
export type Confirmation = components['schemas']['ConfirmationOut'];
export type Plant = components['schemas']['PlantView'];

export const transparencyPort = defineScreenPort('SCR-16', {
  // GET /hierarchy no requiere clave de permiso: basta la sesión. Da zonas y zonas horarias.
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  transparency: {
    kind: 'read',
    status: 'fixed',
    operationId: 'transparency_zones__zone_id__transparency_get',
    permission: 'transparency.read',
  },
  confirm: {
    kind: 'write',
    status: 'fixed',
    operationId: 'confirm_agreement_use_agreements__agreement_id__confirmations_post',
    permission: 'transparency.read',
  },
});

export function transparencyClock() {
  return currentTransport().clock;
}

export function transparencyFailure(error: Error): {
  state: 'error' | 'unavailable' | 'not_available_in_scope';
  correlationId?: string;
} {
  if (!isPortError(error)) return { state: 'error' };
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return { state: 'unavailable' };
  if (failure.kind === 'http') {
    if (failure.behaviour.kind === 'not_available_in_scope')
      return { state: 'not_available_in_scope' };
    const body: unknown = failure.error.body;
    if (
      typeof body === 'object' &&
      body !== null &&
      'correlation_id' in body &&
      typeof body.correlation_id === 'string'
    )
      return { state: 'error', correlationId: body.correlation_id };
  }
  return { state: 'error' };
}

/**
 * Aviso del rechazo de la confirmación (`signatory_not_expected`, `conflict`…) con su etiqueta
 * del mapa cerrado de errores, anclado al formulario; `null` si el fallo no es del servidor.
 */
export function confirmNotice(error: Error | null): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const detail = error.failure.error.detail_code ?? error.failure.error.code;
  return { id: `scr16:${detail}`, error: error.failure.error, field: 'form' };
}

/**
 * Estado de compuertas para `GateStateChip`, tal como lo decidió el servidor (montaje, uso y
 * modo resultante). La vista de transparencia no trae el estado de la regresión.
 */
export function gateDisplay(gates: TransparencyGates): {
  state: string;
  agreement_revoked_at?: string | null;
} {
  if (gates.usage.status === 'revoked')
    return { state: 'agreement_revoked', agreement_revoked_at: gates.usage.decided_at };
  if (gates.mounting.status !== 'approved') return { state: 'no_scope_record' };
  if (gates.resulting_mode === 'productive') return { state: 'productive' };
  return { state: gates.usage.agreement_id === null ? 'no_use_agreement' : 'commissioning' };
}
