/** RV-18 acotada para ejercitar también el perfil nightly sin repetir todas las pantallas. */
import { describe, expect, it } from 'vitest';
import {
  assertNoPersonData,
  assertOrganizationIsolation,
  personFieldsDeclared,
} from '../support/viewProperties';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';
import registrations from '../views/scr14';

describe('RV-18 de SCR-14', () => {
  it(
    'solo pinta campos explícitos del expediente, no identidades observadas',
    () => {
      for (const registration of Object.values(registrations)) {
        expect(personFieldsDeclared(registration)).toEqual([]);
        assertNoPersonData(registration);
      }
    },
    PROPERTY_TIMEOUT_MS,
  );
  it(
    'rechaza los puntos de control de otra organización',
    () => {
      assertOrganizationIsolation(registrations.checkpoints);
    },
    PROPERTY_TIMEOUT_MS,
  );
});
