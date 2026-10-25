/**
 * RV-20 usa los componentes de producción de SCR-16: la vista de transparencia con el resumen de
 * «confirmar mi firma» y el acuse de la confirmación. Ningún hallazgo, contador ni dato de gestión.
 */
import { createElement } from 'react';
import { ConfirmReceipt, ConfirmSummary } from '@/pages/scr16/zone/ConfirmSignature';
import { TransparencyView } from '@/pages/scr16/zone/TransparencyView';
import type { Confirmation, Transparency } from '@/ports/scr16Transparency';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

const TIME_ZONE = 'America/Bogota';

export default {
  transparency: {
    screen: 'SCR-16',
    view: 'RV-20',
    data: readView('TransparencyOut'),
    render: (data) => {
      const transparency = data as unknown as Transparency;
      return createElement(TransparencyView, {
        transparency,
        timeZone: TIME_ZONE,
        confirmation: createElement(ConfirmSummary, { transparency }),
      });
    },
    allowedFields: [
      'declared_scope',
      'scope_text_es',
      'cameras',
      'framing_description_es',
      'reference_marker',
      'minimum_coverage',
      'required_count',
      'standards',
      'title_es',
      'declared_text',
      'version',
      'current_agreement',
      'status',
      'signatories',
      'role',
      'display_name',
      'confirmed_at',
      'created_at',
      'approved_at',
      'gates',
      'mounting',
      'usage',
      'decided_at',
      'resulting_mode',
    ],
    // TransparencyOut no lleva organization_id: el servidor acota la zona por sesión o concesión.
  },
  confirmation: {
    screen: 'SCR-16',
    view: 'RV-20',
    data: readView('ConfirmationOut'),
    render: (data) =>
      createElement(ConfirmReceipt, {
        receipt: data as unknown as Confirmation,
        timeZone: TIME_ZONE,
      }),
    allowedFields: ['confirmed_at', 'role_in_use'],
  },
} satisfies Record<string, ViewRegistration>;
