/**
 * Puerto de SCR-05, gestor de actas, acuerdos y política de incerrables (IF-11, RV-08; U-03 §3.2,
 * §3.7 y «Versión 1.5»).
 *
 * Lecturas (`catalog.read`): compuertas de la zona, política de incerrables y política de
 * firmantes de la planta, y el catálogo vigente de la zona (las cámaras declaradas que el acta de
 * alcance encuadra). La vista de transparencia (`transparency.read`, que tienen todos los roles
 * firmantes) da el acuerdo vigente con sus firmas y `pending_confirmation_for_me`: el único dato
 * del servidor que dice si la persona autenticada tiene una firma pendiente (BR-APP-52).
 *
 * Escrituras (`commissioning.run`): política de incerrables, acta de alcance, política de
 * firmantes, acuerdo de uso, aprobación y revocación de compuerta; y la confirmación de la propia
 * firma (`agreements.sign`), que va **sin cuerpo**: el servidor confirma al usuario de la sesión y
 * nadie puede firmar por otro. Los documentos se suben con `DirectUploadField` (`ports.upload`,
 * `POST /documents`). El permiso y el alcance los decide el servidor (BR-APP-43).
 */
import type { components } from '@/client/schema';
import type { ZoneCatalog } from '@/contracts';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type GateState = components['schemas']['GateStateOut'];
export type PlantPolicy = components['schemas']['PlantPolicyOut'];
export type PlantPolicyBody = components['schemas']['PlantPolicyBody'];
export type SignatoryPolicy = components['schemas']['SignatoryPolicyOut'];
export type SignatoryPolicyBody = components['schemas']['SignatoryPolicyBody'];
export type ScopeRecordBody = components['schemas']['ScopeRecordBody'];
export type UseAgreementBody = components['schemas']['UseAgreementBody'];
export type Agreement = components['schemas']['AgreementOut'];
export type Approval = components['schemas']['ApprovalOut'];
export type Confirmation = components['schemas']['ConfirmationOut'];
export type Transparency = components['schemas']['TransparencyOut'];
export type CatalogVersion = components['schemas']['CatalogVersionOut'];
export type GateKind = components['schemas']['GateKind'];
export type Role = components['schemas']['Role'];
export type Plant = components['schemas']['PlantView'];
export type Zone = components['schemas']['ZoneView'];

/** Roles que una política de firmantes puede exigir (lista cerrada `Role` de U-02). */
export const SIGNATORY_ROLES: readonly Role[] = [
  'coordinator_sst',
  'line_manager',
  'plant_manager',
  'administrator',
  'provider_installer',
  'copasst',
];

/**
 * catalog.domain.gates: el motivo de revocación tiene de 10 a 500 caracteres. OpenAPI no publica
 * estos límites de dominio (`RevocationBody.reason_es` es un texto sin tope en el esquema).
 */
export const REVOCATION_REASON_LIMITS = { minLength: 10, maxLength: 500 } as const;

export const gatesPort = defineScreenPort('SCR-05', {
  // GET /hierarchy no requiere clave de permiso: basta la sesión. Da plantas, zonas y husos.
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  /** `GET /zones/{zone_id}/gates`: montaje, uso y modo resultante de la zona. */
  gates: {
    kind: 'read',
    status: 'fixed',
    operationId: 'gate_state_zones__zone_id__gates_get',
    permission: 'catalog.read',
  },
  /** `GET /zones/{zone_id}/transparency`: acuerdo vigente y firma pendiente de la sesión. */
  transparency: {
    kind: 'read',
    status: 'fixed',
    operationId: 'transparency_zones__zone_id__transparency_get',
    permission: 'transparency.read',
  },
  /** `GET /zones/{zone_id}/catalog`: cámaras declaradas que el acta encuadra. */
  catalog: {
    kind: 'read',
    status: 'fixed',
    operationId: 'current_catalog_zones__zone_id__catalog_get',
    permission: 'catalog.read',
  },
  /** `GET /plants/{plant_id}/policy`: política de incerrables vigente (`loaded = false` sin ella). */
  plantPolicy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'plant_policy_plants__plant_id__policy_get',
    permission: 'catalog.read',
  },
  /** `POST /plants/{plant_id}/policy`: carga la política firmada (versión anterior + 1). */
  signPlantPolicy: {
    kind: 'write',
    status: 'fixed',
    operationId: 'sign_policy_plants__plant_id__policy_post',
    permission: 'commissioning.run',
  },
  /** `GET /plants/{plant_id}/signatory-policy`: roles y mínimo de firmantes de la planta. */
  signatoryPolicy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'signatory_policy_plants__plant_id__signatory_policy_get',
    permission: 'catalog.read',
  },
  /** `PUT /plants/{plant_id}/signatory-policy`. */
  putSignatoryPolicy: {
    kind: 'write',
    status: 'fixed',
    operationId: 'put_signatory_policy_plants__plant_id__signatory_policy_put',
    permission: 'commissioning.run',
  },
  /** `POST /zones/{zone_id}/gates/mounting/scope-record`: acta de alcance (sin clip, D-2). */
  scopeRecord: {
    kind: 'write',
    status: 'fixed',
    operationId: 'scope_record_zones__zone_id__gates_mounting_scope_record_post',
    permission: 'commissioning.run',
  },
  /** `POST /zones/{zone_id}/use-agreements`: acuerdo en `pending_signatures`. */
  createAgreement: {
    kind: 'write',
    status: 'fixed',
    operationId: 'create_agreement_zones__zone_id__use_agreements_post',
    permission: 'commissioning.run',
  },
  /** `POST /use-agreements/{agreement_id}/confirmations`, sin cuerpo: la firma de la sesión. */
  confirm: {
    kind: 'write',
    status: 'fixed',
    operationId: 'confirm_agreement_use_agreements__agreement_id__confirmations_post',
    permission: 'agreements.sign',
  },
  /** `POST /use-agreements/{agreement_id}/approval`: compuerta de uso aprobada. */
  approve: {
    kind: 'write',
    status: 'fixed',
    operationId: 'approve_agreement_use_agreements__agreement_id__approval_post',
    permission: 'commissioning.run',
  },
  /** `POST /zones/{zone_id}/gates/{gate}/revocation`: revoca montaje o uso con motivo. */
  revoke: {
    kind: 'write',
    status: 'fixed',
    operationId: 'revocation_zones__zone_id__gates__gate__revocation_post',
    permission: 'commissioning.run',
  },
});

export function gatesClock() {
  return currentTransport().clock;
}

/** Estado común de una lectura fallida (LC-APP-20). */
export function gatesFailure(error: Error): {
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
 * El aviso del rechazo de una escritura anclado a `field` (errores de §4 de las interfaces: el
 * mensaje sale del mapa cerrado y de las etiquetas `catalog_*`); `null` si no hubo error o si no
 * es una respuesta del servidor.
 */
export function gatesNotice(error: Error | null, field: string): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const detail = error.failure.error.detail_code ?? error.failure.error.code;
  return { id: `scr05:${field}:${detail}`, error: error.failure.error, field };
}

/** La escritura falló sin respuesta del servidor (sin conexión, tiempo agotado, cuerpo inválido). */
export function localFailure(error: Error | null): 'unavailable' | 'error' | null {
  if (error === null) return null;
  if (isPortError(error) && error.failure.kind === 'http') return null;
  if (
    isPortError(error) &&
    (error.failure.kind === 'offline' ||
      error.failure.kind === 'unavailable' ||
      error.failure.kind === 'timeout' ||
      error.failure.kind === 'network')
  )
    return 'unavailable';
  return 'error';
}

/**
 * `GateDisplay` (domain-entities §2.17) para `GateStateChip`, tal como lo decidió el servidor:
 * montaje, uso y modo resultante. Esta lectura no trae la marca de regresión.
 */
export function gateDisplay(gates: Pick<GateState, 'mounting' | 'usage' | 'resulting_mode'>): {
  state: string;
  agreement_revoked_at?: string | null;
} {
  if (gates.usage.status === 'revoked')
    return { state: 'agreement_revoked', agreement_revoked_at: gates.usage.decided_at };
  if (gates.mounting.status !== 'approved') return { state: 'no_scope_record' };
  if (gates.resulting_mode === 'productive') return { state: 'productive' };
  return { state: gates.usage.agreement_id === null ? 'no_use_agreement' : 'commissioning' };
}

/** Una cámara declarada en el catálogo vigente de la zona. */
export interface DeclaredCamera {
  readonly camera_id: string;
  readonly code: string;
}

/** Las cámaras del catálogo vigente; los objetos sin `camera_id` se ignoran. */
export function catalogCameras(version: CatalogVersion | undefined): DeclaredCamera[] {
  const catalog = version?.catalog as Partial<ZoneCatalog> | undefined;
  const cameras: unknown = catalog?.cameras;
  if (!Array.isArray(cameras)) return [];
  return cameras.flatMap((value: unknown) => {
    if (typeof value !== 'object' || value === null) return [];
    const camera = value as Record<string, unknown>;
    if (typeof camera.camera_id !== 'string') return [];
    return [
      {
        camera_id: camera.camera_id,
        code: typeof camera.code === 'string' ? camera.code : camera.camera_id,
      },
    ];
  });
}
