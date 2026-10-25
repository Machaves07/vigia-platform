/** PR-APP-25: oráculo independiente; detección no recarga y dirty exige decisión explícita. */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { createAppVersion } from '@/app/version';
import { createFormDraft, formDraftReducer } from '@/state/reducers/formDraft';

const versionPair = fc.oneof(
  fc.string().map((version) => [version, version] as const),
  fc.tuple(fc.string(), fc.string()),
);
const action = fc.constantFrom('detect', 'edit', 'click', 'cancel', 'confirm_discard', 'navigate');

describe('PR-APP-25', () => {
  it('solo recarga por acción, nunca dirty, conserva ruta completa y borrador hasta descarte confirmado', () => {
    fc.assert(
      fc.property(
        versionPair,
        fc.boolean(),
        fc.string(),
        fc.string(),
        fc.array(action, { maxLength: 60 }),
        ([local, announced], dirty, value, segment, actions) => {
          let draft = {
            ...createFormDraft('SCR-08', 'synthetic'),
            values: { reason: value },
            dirty,
          };
          let route = `/findings/${encodeURIComponent(segment)}?page=2#details`;
          let reloads = 0;
          let userAction = false;
          let expectedReloads = 0;
          const outdated = local !== announced;
          const version = createAppVersion({
            appVersion: local,
            contractTag: 'v1.1.0',
            reload: (context) => {
              expect(userAction).toBe(true);
              expect(context.draft?.dirty).toBe(false);
              expect(context.route).toBe(route);
              reloads += 1;
            },
          });
          const original = draft;
          version.observeApiVersion(announced);
          expect(reloads).toBe(0);
          expect(draft).toBe(original);
          expect(version.canWrite()).toBe(!outdated);

          for (const event of actions) {
            const before = draft;
            const beforeRoute = route;
            switch (event) {
              case 'detect':
                version.observeMe({ api_version: announced });
                // Una respuesta antigua nunca levanta el bloqueo.
                version.observeApiVersion(local);
                break;
              case 'edit':
                draft = { ...draft, dirty: true };
                break;
              case 'navigate':
                route = `${beforeRoute}/next`;
                break;
              case 'cancel':
                break;
              case 'confirm_discard':
                // Acción distinta: la confirmación por sí sola tampoco recarga.
                draft = { ...formDraftReducer(draft, { type: 'discard' }), values: { reason: '' } };
                break;
              case 'click': {
                userAction = true;
                if (outdated && !draft.dirty) expectedReloads += 1;
                expect(version.requestReload({ route, draft })).toBe(
                  !outdated ? 'not_needed' : draft.dirty ? 'confirmation_required' : 'reloading',
                );
                userAction = false;
                break;
              }
            }
            if (event === 'detect' || event === 'cancel' || event === 'click') {
              expect(draft).toBe(before);
              expect(route).toBe(beforeRoute);
            }
            expect(reloads).toBe(expectedReloads);
            expect(version.canWrite()).toBe(!outdated);
          }
        },
      ),
    );
  });
});
