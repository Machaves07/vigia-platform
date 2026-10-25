/** Sondas acotadas de RV-05/06 para poder repetirlas con el perfil nocturno sin toda la suite. */
import { describe, expect, it } from 'vitest';
import { assertNoPersonData, assertOrganizationIsolation } from '../support/viewProperties';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';
import views from '../views/scr03';

describe('SCR-03: metapropiedades de las vistas reales', () => {
  it.each(Object.entries(views))(
    '%s no pinta campos fuera de su vista',
    (_name, registration) => {
      assertNoPersonData(registration);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'puntos de control y cobertura rechazan organizaciones ajenas y admiten la concesión',
    () => {
      expect(assertOrganizationIsolation(views.integrityCheckpoints)).toBeGreaterThan(0);
      expect(assertOrganizationIsolation(views.zoneCoverage)).toBeGreaterThan(0);
    },
    PROPERTY_TIMEOUT_MS,
  );
});
