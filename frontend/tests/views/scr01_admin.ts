/**
 * RV-01 (cuenta y sesiones) y RV-02 (usuarios) de SCR-01 (VIG-305), con los componentes de
 * producción. La cuenta pinta nombre, correo, organización, licencia, segundo factor inscrito y
 * asignaciones; las sesiones, `client_hint`, fechas y estado (nunca un identificador, que no
 * existe en el cliente); los usuarios, nombre, correo, estado, licencia, segundo factor y
 * asignaciones. Ninguna lleva `organization_id` por registro: el servidor acota a la organización
 * de la sesión y PR-APP-10 no las recorre.
 *
 * El `email` de estas dos vistas es el de la cuenta de la plataforma (domain-entities.md §3), no
 * el de una persona observada: `personFieldsDeclared` lo admite solo para RV-01 y RV-02 de SCR-01.
 * Con la jerarquía vacía, ningún identificador de alcance llega a pintarse.
 */
import { createElement } from 'react';

import { AccountProfile, SessionsList } from '@/pages/scr01/account/AccountView';
import { UsersRows } from '@/pages/scr01/users/UsersView';
import type { AccountSession, ManagedUser } from '@/ports/scr01Admin';
import type { MeResponse } from '@/ports/shared';

import { readView } from '../generators';
import type { ViewRegistration } from './types';

const ASSIGNMENT_FIELDS = ['assignments', 'role', 'scope_level', 'scope_id'];
const NO_SCOPES = { organization: null, plants: [] };

export default {
  account_profile: {
    screen: 'SCR-01',
    view: 'RV-01',
    data: readView('MeResponse'),
    render: (data) => {
      const me = data as MeResponse;
      return createElement(AccountProfile, {
        account: {
          display_name: me.user.display_name,
          email: me.user.email,
          organization: me.organization,
          professional_license: me.professional_license,
          second_factor_enrolled: me.second_factor_enrolled,
          assignments: me.assignments,
        },
        directory: NO_SCOPES,
      });
    },
    allowedFields: [
      'user',
      'display_name',
      'email',
      'organization',
      'name',
      'professional_license',
      'second_factor_enrolled',
      ...ASSIGNMENT_FIELDS,
    ],
  },
  account_sessions: {
    screen: 'SCR-01',
    view: 'RV-01',
    data: readView('SessionsResponse'),
    render: (data) =>
      createElement(SessionsList, {
        sessions: data.sessions as AccountSession[],
        timeZone: 'America/Bogota',
      }),
    allowedFields: ['sessions', 'client_hint', 'created_at', 'last_seen_at', 'current', 'usable'],
  },
  users: {
    screen: 'SCR-01',
    view: 'RV-02',
    data: readView('UsersPage'),
    render: (data) =>
      createElement(UsersRows, { users: data.users as ManagedUser[], directory: NO_SCOPES }),
    allowedFields: [
      'users',
      'display_name',
      'email',
      'status',
      'professional_license',
      'second_factor_enrolled',
      ...ASSIGNMENT_FIELDS,
    ],
  },
} satisfies Record<string, ViewRegistration>;
