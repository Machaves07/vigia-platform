/**
 * RV-08 usa los componentes de producción de SCR-05: estado de compuertas, acuerdo con sus firmas,
 * política de firmantes, política de incerrables, el resumen de «confirmar mi firma» y su acuse.
 * Ningún hallazgo ni contador; los identificadores (`decided_by`, `record_id`, `agreement_id`,
 * `user_id`) relacionan datos y nunca se pintan.
 */
import { createElement } from 'react';
import { ConfirmReceipt, ConfirmSummary } from '@/pages/scr05/gates/ConfirmSignature';
import {
  AgreementView,
  GateStatePanel,
  PlantPolicyView,
  SignatoryPolicyView,
} from '@/pages/scr05/views';
import type {
  Agreement,
  Confirmation,
  GateState,
  PlantPolicy,
  SignatoryPolicy,
  Transparency,
} from '@/ports/scr05Gates';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

const TIME_ZONE = 'America/Bogota';

// Ninguna de estas respuestas lleva organization_id: el servidor acota la zona y la planta por
// sesión o concesión, así que PR-APP-10 no las recorre.
export default {
  scr05_gates: {
    screen: 'SCR-05',
    view: 'RV-08',
    data: readView('GateStateOut'),
    render: (data) =>
      createElement(GateStatePanel, { gates: data as unknown as GateState, timeZone: TIME_ZONE }),
    allowedFields: ['mounting', 'usage', 'status', 'decided_at', 'resulting_mode', 'issued_at'],
  },
  scr05_agreement: {
    screen: 'SCR-05',
    view: 'RV-08',
    data: readView('AgreementOut'),
    render: (data) =>
      createElement(AgreementView, {
        agreement: data as unknown as Agreement,
        timeZone: TIME_ZONE,
        caption: 'Firmantes',
      }),
    allowedFields: [
      'status',
      'created_at',
      'approved_at',
      'signatories',
      'role',
      'display_name',
      'confirmed_at',
    ],
  },
  scr05_signatory_policy: {
    screen: 'SCR-05',
    view: 'RV-08',
    data: readView('SignatoryPolicyOut'),
    render: (data) =>
      createElement(SignatoryPolicyView, {
        policy: data as unknown as SignatoryPolicy,
        timeZone: TIME_ZONE,
      }),
    allowedFields: ['configured', 'required_roles', 'minimum', 'updated_at'],
  },
  scr05_plant_policy: {
    screen: 'SCR-05',
    view: 'RV-08',
    data: readView('PlantPolicyOut'),
    render: (data) =>
      createElement(PlantPolicyView, {
        policy: data as unknown as PlantPolicy,
        timeZone: TIME_ZONE,
      }),
    allowedFields: [
      'loaded',
      'version',
      'signed_at',
      'signed_by_display_name',
      'legal_opinion_reference',
      'criteria_summary_es',
    ],
  },
  scr05_confirm_summary: {
    screen: 'SCR-05',
    view: 'RV-08',
    data: readView('TransparencyOut'),
    render: (data) =>
      createElement(ConfirmSummary, { transparency: data as unknown as Transparency }),
    allowedFields: ['declared_scope', 'scope_text_es', 'standards', 'title_es'],
  },
  scr05_confirmation: {
    screen: 'SCR-05',
    view: 'RV-08',
    data: readView('ConfirmationOut'),
    render: (data) =>
      createElement(ConfirmReceipt, {
        receipt: data as unknown as Confirmation,
        timeZone: TIME_ZONE,
      }),
    allowedFields: ['confirmed_at', 'role_in_use'],
  },
} satisfies Record<string, ViewRegistration>;
