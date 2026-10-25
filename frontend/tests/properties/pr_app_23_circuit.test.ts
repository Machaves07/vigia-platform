import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { DEPENDENCIES, type Dependency } from '@/state/connection/reducer';

import { fixture } from '../support/connection';

const dependency = fc.constantFrom(...DEPENDENCIES);
const outcome = fc.constantFrom(
  ...([
    'success',
    'timeout',
    'rate_limited',
    'business_503',
    'probe_ok',
    'probe_fail',
    'tick',
  ] as const),
);

describe('PR-APP-23', () => {
  it('coincide tras cada evento con un modelo temporal independiente por dependencia', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            dep: dependency,
            outcome,
            elapsed: fc.integer({ min: 0, max: 40_000 }),
          }),
          { maxLength: 120 },
        ),
        (events) => {
          const { clock, connection } = fixture();
          interface Model {
            failures: number[];
            blockedUntil: number | null;
            recovering: boolean;
          }
          const fresh = (): Model => ({ failures: [], blockedUntil: null, recovering: false });
          const model: Record<Dependency, Model> = {
            platform: fresh(),
            evidence_store: fresh(),
            document_store: fresh(),
            live_view_node: fresh(),
          };
          let now = 0;
          for (const event of events) {
            now += event.elapsed;
            clock.advance(event.elapsed);
            for (const value of Object.values(model)) {
              // Ventana de 60 s: cabe el ciclo de una lectura que agota sus reintentos (U05-H-10).
              value.failures = value.failures.filter((at) => at > now - 60_000);
              if (value.blockedUntil !== null && value.blockedUntil <= now) {
                value.blockedUntil = null;
                value.recovering = true;
              }
            }
            const current = model[event.dep];
            if (event.outcome === 'success' || event.outcome === 'probe_ok') {
              current.failures = [];
              current.blockedUntil = null;
              current.recovering = false;
            } else if (event.outcome === 'timeout' || event.outcome === 'probe_fail') {
              if (current.blockedUntil === null) {
                current.failures.push(now);
                if (current.recovering || current.failures.length === 4) {
                  current.blockedUntil = now + 30_000;
                  current.recovering = false;
                  current.failures = [];
                }
              }
            }
            switch (event.outcome) {
              case 'tick':
                connection.tick();
                break;
              case 'probe_ok':
                connection.registerProbe(event.dep, true);
                break;
              case 'probe_fail':
                connection.registerProbe(event.dep, false);
                break;
              case 'success':
                connection.register(event.dep, { ok: true });
                break;
              default:
                connection.register(event.dep, { ok: false, error: event.outcome });
            }
            const snapshot = connection.getSnapshot();
            for (const dep of DEPENDENCIES) {
              const expected =
                model[dep].blockedUntil !== null
                  ? 'open'
                  : model[dep].recovering
                    ? 'half_open'
                    : 'closed';
              expect(snapshot.circuits[dep].state).toBe(expected);
              expect(connection.canRead(dep)).toBe(expected !== 'open');
            }
            expect(connection.canWrite()).toBe(model.platform.blockedUntil === null);
            expect(snapshot.connection.state).toBe(
              model.platform.blockedUntil !== null
                ? 'offline'
                : Object.values(model).some(
                      (value) =>
                        value.blockedUntil !== null ||
                        value.recovering ||
                        value.failures.length > 0,
                    )
                  ? 'degraded'
                  : 'online',
            );
          }
        },
      ),
    );
  });

  it('abre solo en el cuarto fallo y bloquea escritura solo para platform', () => {
    fc.assert(
      fc.property(dependency, (dep) => {
        const { connection, fail } = fixture();
        for (let i = 1; i <= 4; i++) {
          fail(dep);
          expect(connection.getSnapshot().circuits[dep].state).toBe(i === 4 ? 'open' : 'closed');
          expect(connection.canWrite()).toBe(!(dep === 'platform' && i === 4));
        }
        connection.registerProbe(dep, true);
        expect(connection.getSnapshot().circuits[dep].state).toBe('closed');
      }),
    );
  });

  it('rate_limited y los 503 de negocio nunca abren', () => {
    fc.assert(
      fc.property(
        dependency,
        fc.array(fc.constantFrom('rate_limited' as const, 'business_503' as const), {
          minLength: 4,
          maxLength: 100,
        }),
        (dep, errors) => {
          const { connection } = fixture();
          for (const error of errors) connection.register(dep, { ok: false, error });
          expect(connection.getSnapshot().circuits[dep].state).toBe('closed');
        },
      ),
    );
  });
});
