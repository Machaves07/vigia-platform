/**
 * PR-APP-10 (BR-APP-30; NFR-APP-45, bloqueante): para respuestas con `organization_id`
 * mezclados, cada vista registrada en `tests/views/` con `organizationOf` solo pinta los
 * registros de la organización del `Principal` o de la concesión activa y registra el resto
 * como error de contrato. Corre también sobre la vista de autoprueba y sus mutaciones, que caen.
 */
import { describe, expect, it } from 'vitest';

import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';
import { assertOrganizationIsolation } from '../support/viewProperties';
import { views } from '../views';
import { renderSelftest, selftestView } from '../views/_selftest';

const scoped = Object.values(views).filter(
  (registration) => registration.organizationOf !== undefined,
);
const count = `${String(scoped.length)} vistas`;

describe('PR-APP-10: ninguna pantalla pinta datos de otra organización', () => {
  it(
    `vistas registradas en tests/views con organizationOf (${count})`,
    () => {
      process.stdout.write(`PR-APP-10: ${count}\n`);
      for (const registration of scoped) {
        expect(assertOrganizationIsolation(registration), registration.view).toBeGreaterThan(0);
      }
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'vista de autoprueba',
    () => {
      expect(assertOrganizationIsolation(selftestView)).toBeGreaterThan(0);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cae en rojo nombrando la vista si pinta registros de otra organización',
    () => {
      expect(() =>
        assertOrganizationIsolation({
          ...selftestView,
          render: (data, context) => renderSelftest(data, context, { filterOrganization: false }),
        }),
      ).toThrow(/RV-00 \(SCR-00\): \d+ registros de otra organización y ningún error de contrato/);
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cae en rojo aunque registre el error de contrato si aun así pinta el registro ajeno',
    () => {
      expect(() =>
        assertOrganizationIsolation({
          ...selftestView,
          render: (data, context) => {
            renderSelftest(data, context);
            return renderSelftest(
              data,
              { ...context, reportContractError: () => undefined },
              { filterOrganization: false },
            );
          },
        }),
      ).toThrow(
        /RV-00 \(SCR-00\): «items\[\d+\]\.title» de un registro de otra organización aparece/,
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
