/**
 * BR-APP-07 y NFR-APP-10: toda lista paginada de los puertos declarados envía el alcance elegido
 * como filtro explícito (`plant_id` y, si la operación lo admite, `zone_id`) y nunca la
 * organización. Se recorren los puertos de `src/ports/scrNN*` y `src/ports/shared/`; cada lectura cuya operación de
 * `app.yaml` admite `plant_id` se llama con `scopeParams` de su pantalla y MSW anota la consulta
 * que llega. Con cero puertos de lista declarados, esa prueba se salta con su motivo; la del
 * mecanismo, sobre un puerto declarado aquí, corre siempre.
 */
import { HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';

import {
  createPortConnection,
  createTransport,
  defineScreenPort,
  installTransport,
} from '@/ports/base';
import { SCREENS, type Screen } from '@/state/query';
import { createPreferencesStore, createScopeStore, type ScopeStore } from '@/state/scope';

import { variant } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';

const PLANT = '7c9e6679-7425-40de-944b-e07fc1f90ae7';
const ZONE = '16fd2706-8baf-433b-82eb-8c7fada847da';
/** Parámetros de ruta sintéticos para las listas anidadas (`/zones/{zone_id}/…`). */
const PATH = new Proxy({}, { get: () => ZONE, has: () => true }) as Record<string, string>;

interface QuerySchema {
  readonly query: { readonly properties: Readonly<Record<string, unknown>> } | null;
}

const BODIES = import.meta.glob<QuerySchema>('../../../openapi/bodies/*.json', {
  eager: true,
  import: 'default',
});

function queryParameters(operationId: string): readonly string[] {
  const schema = BODIES[`../../../openapi/bodies/${operationId}.json`];
  return Object.keys(schema?.query?.properties ?? {});
}

interface OperationInfo {
  readonly name: string;
  readonly kind: 'read' | 'write';
  readonly api_operation_id: string | null;
}
type Read = (params?: unknown, signal?: AbortSignal) => Promise<unknown>;
interface DeclaredPort {
  readonly screen: string;
  readonly operations: readonly OperationInfo[];
  readonly [name: string]: unknown;
}

function isPort(value: unknown): value is DeclaredPort {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { screen?: unknown }).screen === 'string' &&
    Array.isArray((value as { operations?: unknown }).operations)
  );
}

const MODULES = {
  ...import.meta.glob('../../../src/ports/scr*.ts', { eager: true }),
  ...import.meta.glob('../../../src/ports/scr*/**/*.ts', { eager: true }),
  ...import.meta.glob('../../../src/ports/shared/**/*.ts', { eager: true }),
};
const PORTS = Object.values(MODULES).flatMap((module) =>
  Object.values(module as Record<string, unknown>).filter(isPort),
);
const LISTS = PORTS.flatMap((port) =>
  port.operations
    .filter(
      (operation) =>
        operation.kind === 'read' &&
        operation.api_operation_id !== null &&
        queryParameters(operation.api_operation_id).includes('plant_id'),
    )
    .map((operation) => ({ port, operation })),
);

function screenOf(port: DeclaredPort): Screen {
  const screen = port.screen.replace(/^SCR-/, 'scr');
  expect(SCREENS).toContain(screen);
  return screen as Screen;
}

function scopeWith(plant: string | null, zone: { screen: Screen; id: string } | null): ScopeStore {
  const store = createScopeStore(createPreferencesStore(null));
  store.setAvailablePlants([
    { plant_id: PLANT, code: 'N', name: 'Norte', timezone: 'America/Bogota', country: 'CO' },
  ]);
  if (plant !== null) store.selectPlant(plant);
  if (zone !== null) store.setZone(zone.screen, zone.id);
  return store;
}

/**
 * Instala el transporte real sobre MSW y anota la URL de cada petición a `operationId`, que
 * responde con un cuerpo mínimo (lo que se prueba es la consulta, no la respuesta).
 */
function recordRequests(operationId: string): URL[] {
  const clock = new FakeClock();
  installTransport(
    createTransport({
      clock,
      connection: createPortConnection(clock),
      devLog: () => undefined,
    }),
  );
  const urls: URL[] = [];
  server.use(
    variant(operationId, ({ request }) => {
      urls.push(new URL(request.url));
      return HttpResponse.json({});
    }),
  );
  return urls;
}

afterEach(() => {
  installTransport(null);
});

describe('filtro de alcance explícito en toda lista (BR-APP-07, NFR-APP-10)', () => {
  it.skipIf(LISTS.length === 0)(
    `cada lista paginada de los puertos declarados envía plant_id y zone_id elegidos (${String(LISTS.length)} listas; con 0 se salta: aún no hay puertos de pantalla que declaren listas con plant_id)`,
    async () => {
      for (const { port, operation } of LISTS) {
        const screen = screenOf(port);
        const parameters = queryParameters(operation.api_operation_id ?? '');
        const urls = recordRequests(operation.api_operation_id ?? '');
        const store = scopeWith(PLANT, { screen, id: ZONE });
        const read = (port[operation.name] as { read: Read }).read;
        await read({ path: PATH, query: store.scopeParams(screen) }).catch(() => undefined);
        const url = urls.at(-1);
        expect(url?.searchParams.get('plant_id'), operation.name).toBe(PLANT);
        if (parameters.includes('zone_id')) {
          expect(url?.searchParams.get('zone_id'), operation.name).toBe(ZONE);
        }
        expect(url?.searchParams.has('organization_id'), operation.name).toBe(false);
        server.resetHandlers();
      }
    },
  );

  describe('mecanismo, sobre un puerto declarado aquí con GET /ledger/records', () => {
    const port = defineScreenPort('SCR-03', {
      records: {
        kind: 'read',
        status: 'fixed',
        operationId: 'list_records_ledger_records_get',
        permission: 'ledger.read',
      },
    });

    it('con una planta y una zona de la pantalla, las dos viajan al servidor', async () => {
      const urls = recordRequests('list_records_ledger_records_get');
      const store = scopeWith(PLANT, { screen: 'scr03', id: ZONE });
      await port.records.read({ query: { page_size: 50, ...store.scopeParams('scr03') } });
      expect(urls).toHaveLength(1);
      expect(Object.fromEntries(urls[0]?.searchParams ?? [])).toEqual({
        page_size: '50',
        plant_id: PLANT,
        zone_id: ZONE,
      });
    });

    it('con «todas» no envía planta: el servidor pagina sobre el alcance de la sesión', async () => {
      const urls = recordRequests('list_records_ledger_records_get');
      const store = scopeWith(null, null);
      await port.records.read({ query: { page_size: 50, ...store.scopeParams('scr03') } });
      expect(Object.fromEntries(urls[0]?.searchParams ?? [])).toEqual({ page_size: '50' });
    });

    it('la zona de otra pantalla no se envía: el filtro de zona no es global', async () => {
      const urls = recordRequests('list_records_ledger_records_get');
      const store = scopeWith(PLANT, { screen: 'scr08', id: ZONE });
      await port.records.read({ query: store.scopeParams('scr03') });
      expect(Object.fromEntries(urls[0]?.searchParams ?? [])).toEqual({ plant_id: PLANT });
    });
  });
});
