/** `expect(await axe(container)).toHaveNoViolations()` en las pruebas de componente (NFR-APP-50). */
import { expect } from 'vitest';
import type { AxeMatchers } from 'vitest-axe';
import * as matchers from 'vitest-axe/matchers';

expect.extend(matchers);

declare module 'vitest' {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type -- ampliación del tipo de vitest
  interface Assertion extends AxeMatchers {}
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type -- ampliación del tipo de vitest
  interface AsymmetricMatchersContaining extends AxeMatchers {}
}
