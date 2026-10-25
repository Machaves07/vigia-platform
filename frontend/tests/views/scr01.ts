/**
 * RV-01, parte de sesión (SCR-01, VIG-321): lo que las rutas públicas pintan de las respuestas del
 * servidor, con los componentes de producción. La inscripción del segundo factor
 * (`EnrollResponse`) y la activación por invitación (`ActivationResponse`) solo muestran el QR,
 * la clave a mano, los códigos de recuperación y el aviso con su versión; nunca otro campo.
 * Ninguna lleva `organization_id`: PR-APP-10 no las recorre.
 */
import { createElement, Fragment, type ReactElement } from 'react';

import { NoticeBody } from '@/pages/scr01/privacyNotice';
import { QrCode, RecoveryCodes } from '@/pages/scr01/EnrollmentSecrets';
import type { ActivationResponse, EnrollmentView } from '@/ports/scr01Session';

import { readView } from '../generators';
import type { ViewRegistration } from './types';

const ENROLLMENT_FIELDS = ['enrollment', 'provisioning_uri', 'qr_svg', 'recovery_codes'];

function enrollment(view: EnrollmentView | null | undefined): ReactElement | null {
  if (view == null) return null;
  return createElement(
    Fragment,
    null,
    createElement(QrCode, { enrollment: view }),
    createElement(RecoveryCodes, { codes: view.recovery_codes, onAcknowledged: () => undefined }),
  );
}

export default {
  session_enrollment: {
    screen: 'SCR-01',
    view: 'RV-01',
    data: readView('EnrollResponse'),
    render: (data) => {
      const response = data as { enrollment?: EnrollmentView | null };
      return createElement('main', null, enrollment(response.enrollment));
    },
    allowedFields: ['status', ...ENROLLMENT_FIELDS],
  },
  session_activation: {
    screen: 'SCR-01',
    view: 'RV-01',
    data: readView('ActivationResponse'),
    render: (data) => {
      const response = data as ActivationResponse;
      const notice = response.notice ?? null;
      return createElement(
        'main',
        null,
        enrollment(response.enrollment),
        notice === null
          ? null
          : createElement(
              'section',
              null,
              createElement('p', null, notice.version),
              createElement(NoticeBody, { body: notice.text }),
            ),
      );
    },
    allowedFields: [
      'status',
      'second_factor_required',
      ...ENROLLMENT_FIELDS,
      'notice',
      'version',
      'text',
      'pending_legal_text',
    ],
  },
} satisfies Record<string, ViewRegistration>;
