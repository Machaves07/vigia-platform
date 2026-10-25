/** PR-APP-07 y 10 de RV-13, aislables también con el perfil nightly de la unidad. */
import { describe, it } from 'vitest';
import { assertNoPersonData, assertOrganizationIsolation } from '../support/viewProperties';
import registrations from '../views/scr09_closure';

describe('RV-13 de cierre', { timeout: 120_000 }, () => {
  it('solo pinta los campos declarados del historial', () => {
    assertNoPersonData(registrations.history);
  });
  it('solo pinta los campos declarados de vencidos', () => {
    assertNoPersonData(registrations.overdue);
  });
  it('no pinta registros de otra organización y registra el error de contrato', () => {
    assertOrganizationIsolation(registrations.history);
  });
});
