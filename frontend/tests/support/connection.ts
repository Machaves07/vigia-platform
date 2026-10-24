import { createConnection } from '@/state/connection';
import type { Dependency } from '@/state/connection';

import { FakeClock } from './fakeClock';

export type TestError =
  | 'timeout'
  | 'network'
  | 'temporarily_unavailable'
  | 'rate_limited'
  | 'throttled'
  | 'business_503'
  | 'storage_unavailable';

/** Sustituto del clasificador de VIG-281: la integración corresponde a VIG-292. */
export function fixture() {
  const clock = new FakeClock();
  const connection = createConnection<TestError>({
    clock,
    counts_for_circuit: (error, dep) =>
      ['timeout', 'network', 'temporarily_unavailable'].includes(error) ||
      (error === 'storage_unavailable' && dep !== 'platform'),
  });
  const fail = (dep: Dependency = 'platform', error: TestError = 'timeout') => {
    connection.register(dep, { ok: false, error });
  };
  const open = (dep: Dependency = 'platform') => {
    for (let i = 0; i < 4; i++) fail(dep);
  };
  return { clock, connection, fail, open };
}
