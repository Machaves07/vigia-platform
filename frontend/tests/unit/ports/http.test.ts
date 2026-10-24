/**
 * `ports.base` (LC-APP-13 y 14; BR-APP-61 a 63, 67, 69, 70, 72 y 73; NFR-APP-04 y 42; IF-20):
 * topes de 10 y 30 s del `FakeClock` en toda operación declarada, cabeceras con MSW, registro de
 * desarrollo sin cuerpos, traducción de errores, eventos de sesión, circuitos, operaciones
 * `assumed`, validación del cuerpo y carga prolongada.
 */
/* eslint-disable no-restricted-imports -- la prueba recorre las operaciones y tipos del cliente generado. */
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from 'vitest';

import { OPERATIONS } from '@/client';
import type { components } from '@/client/schema';
import { bodies } from '@/client/validators.js';
import {
  API_VERSION_HEADER,
  CONCESSION_HEADER,
  createDevLog,
  createPortConnection,
  createTransport,
  defineScreenPort,
  installTransport,
  SLOW_AFTER_MS,
  TIMEOUT_MS,
  type Declaration,
  type DevLogEntry,
  type OperationId,
  type ScreenPort,
  type TransportOptions,
} from '@/ports/base';
import { behaviourFor } from '@/ports/errors/map';
import { isPortError, PortError } from '@/ports/shared';

import { apiError, mswPath, variant } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { drive, flush, scriptedFetch, silentFetch } from '../../support/ports';

const ALL_IDS = Object.keys(OPERATIONS) as OperationId[];
/**
 * Tope del recorrido de todas las operaciones por MSW (300 peticiones reales). No es lo que se
 * prueba: holgado para una máquina compartida (AGENTS.md: nada de topes cortos).
 */
const MSW_SWEEP_TIMEOUT_MS = 120_000;
const kindOf = (id: OperationId) => (OPERATIONS[id].method === 'get' ? 'read' : 'write');

/** Parámetros de ruta con un valor sintético por nombre. */
function pathParams(id: OperationId, value = 'p-1'): Record<string, string> {
  const names = [...OPERATIONS[id].path.matchAll(/\{([^}]+)\}/g)].map((match) => match[1] ?? '');
  return Object.fromEntries(names.map((name) => [name, value]));
}

function failureOf(outcome: { ok: boolean; error?: unknown }) {
  return !outcome.ok && isPortError(outcome.error) ? outcome.error.failure : null;
}

function transportWith(clock: FakeClock, extra: Partial<TransportOptions> = {}) {
  const connection = createPortConnection(clock);
  const transport = createTransport({ clock, connection, devLog: () => undefined, ...extra });
  return { connection, transport };
}

afterEach(() => {
  installTransport(null);
  server.events.removeAllListeners();
});

describe('tiempos de espera (BR-APP-61, NFR-APP-04)', () => {
  it('toda operación de app.yaml, declarada en un puerto, aborta a los 10 s (lectura) o 30 s (escritura)', async () => {
    const clock = new FakeClock();
    const entries: DevLogEntry[] = [];
    const { fetch } = silentFetch(clock);
    const { transport } = transportWith(clock, {
      fetch,
      devLog: (entry) => entries.push(entry),
    });
    installTransport(transport);
    const declarations = Object.fromEntries(
      ALL_IDS.map((id) => [
        id,
        { kind: kindOf(id), status: 'fixed', operationId: id, permission: 'test' } as Declaration,
      ]),
    );
    const port = defineScreenPort('SCR-00', declarations) as unknown as Record<
      string,
      { read?: (p: unknown) => Promise<unknown>; write?: (p: unknown) => Promise<unknown> }
    >;
    const pending = ALL_IDS.map((id) => {
      const operation = port[id];
      const input = { path: pathParams(id) };
      // Una escritura con cuerpo se valida antes de salir: sin cuerpos válidos de las 66
      // operaciones, va al mismo transporte que usa el puerto, con un cuerpo cualquiera.
      if (Object.hasOwn(bodies, id)) {
        return transport
          .request({ operationId: id, kind: 'write', body: {}, ...input })
          .catch(() => undefined);
      }
      const call = operation?.read ? operation.read(input) : operation?.write?.(input);
      return call?.catch(() => undefined);
    });

    clock.advance(TIMEOUT_MS.read - 1);
    await flush();
    expect(entries).toEqual([]);
    clock.advance(1);
    await flush();
    clock.advance(TIMEOUT_MS.write - TIMEOUT_MS.read);
    await flush();

    for (const id of ALL_IDS) {
      const { method, path } = OPERATIONS[id];
      const first = entries.find((entry) => entry.method === method && entry.route === path);
      expect(first, id).toEqual({
        method,
        route: path,
        status: null,
        code: 'timeout',
        durationMs: TIMEOUT_MS[kindOf(id)],
      });
    }
    expect(pending.every((call) => call !== undefined)).toBe(true);
    await drive(clock, Promise.all(pending.filter((call) => call !== undefined)));
  });

  it('todo puerto de pantalla de src/ports/scr* declara operaciones con tope', async () => {
    const modules = import.meta.glob<Record<string, unknown>>('/src/ports/scr*.ts', {
      eager: true,
    });
    const ports = Object.values(modules)
      .flatMap((module) => Object.values(module))
      .filter(
        (value): value is ScreenPort<Record<string, Declaration>> =>
          typeof value === 'object' && value !== null && 'operations' in value && 'screen' in value,
      );
    for (const port of ports) {
      for (const info of port.operations) {
        if (info.api_operation_id === null) continue;
        const clock = new FakeClock();
        const entries: DevLogEntry[] = [];
        const { fetch } = silentFetch(clock);
        const { transport } = transportWith(clock, {
          fetch,
          devLog: (entry) => entries.push(entry),
        });
        const result = transport
          .request({
            operationId: info.api_operation_id,
            kind: info.kind,
            path: pathParams(info.api_operation_id),
          })
          .catch(() => undefined);
        clock.advance(TIMEOUT_MS[info.kind]);
        await flush();
        expect(entries[0]?.durationMs, `${port.screen}.${info.name}`).toBe(TIMEOUT_MS[info.kind]);
        await drive(clock, result);
      }
    }
  });

  it('con fetch real (MSW): una lectura sin respuesta aborta a los 10 s y una escritura a los 30 s', async () => {
    const clock = new FakeClock();
    const entries: DevLogEntry[] = [];
    server.use(
      variant('me_me_get', () => new Promise<never>(() => undefined)),
      variant('logout_auth_logout_post', () => new Promise<never>(() => undefined)),
    );
    const { transport } = transportWith(clock, { devLog: (entry) => entries.push(entry) });
    const read = transport.request({ operationId: 'me_me_get', kind: 'read' });
    const write = transport.request({ operationId: 'logout_auth_logout_post', kind: 'write' });
    read.catch(() => undefined);
    write.catch(() => undefined);

    await flush();
    clock.advance(TIMEOUT_MS.read - 1);
    await flush();
    expect(entries).toEqual([]);
    clock.advance(1);
    await flush();
    expect(entries).toEqual([
      { method: 'get', route: '/me', status: null, code: 'timeout', durationMs: 10_000 },
    ]);
    clock.advance(TIMEOUT_MS.write - TIMEOUT_MS.read);
    await flush();
    expect(entries.filter((entry) => entry.method === 'post')).toEqual([
      { method: 'post', route: '/auth/logout', status: null, code: 'timeout', durationMs: 30_000 },
    ]);
    const writeOutcome = await drive(clock, write);
    expect(failureOf(writeOutcome)).toEqual({
      kind: 'timeout',
      behaviour: behaviourFor({ code: 'temporarily_unavailable', status: 0 }),
    });
    await drive(clock, read);
  });
});

describe('cabeceras y credenciales (BR-APP-67, 69, 70)', () => {
  it(
    'X-Vigia-Concession en toda petición si y solo si el proveedor devuelve una concesión',
    async () => {
      const seen: { id: string; concession: string | null; credentials: string }[] = [];
      server.events.on('request:start', ({ request }) => {
        seen.push({
          id: `${request.method.toLowerCase()} ${new URL(request.url).pathname}`,
          concession: request.headers.get(CONCESSION_HEADER),
          credentials: request.credentials,
        });
      });
      let current: string | null = null;
      const clock = new FakeClock();
      const { transport } = transportWith(clock, { concession: () => current });

      const expected: (string | null)[] = [];
      for (const [round, values] of [
        ['c-0f9e', null],
        [null, ''],
      ].entries()) {
        for (const [index, id] of ALL_IDS.entries()) {
          const value = values[(index + round) % 2] ?? null;
          current = value;
          expected.push(value === '' ? null : value);
          await transport
            .request({ operationId: id, kind: kindOf(id), path: pathParams(id) })
            .catch(() => undefined);
        }
      }

      expect(seen).toHaveLength(ALL_IDS.length * 2);
      expect(seen.map((request) => request.concession)).toEqual(expected);
      expect(new Set(seen.map((request) => request.credentials))).toEqual(new Set(['same-origin']));
    },
    MSW_SWEEP_TIMEOUT_MS,
  );

  it('el destino es siempre el origen de la aplicación y la plantilla de app.yaml', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock);
    const { transport } = transportWith(clock, { fetch, origin: 'https://app.vigia.test' });
    await transport.request({
      operationId: 'list_users_users_get',
      kind: 'read',
      query: { after: 'a b&c', limit: 50 },
    });
    await transport.request({
      operationId: 'deactivate_users__user_id__deactivate_post',
      kind: 'write',
      path: { user_id: '../../evil.example/x?y' },
    });
    expect(calls.map((call) => call.url)).toEqual([
      'https://app.vigia.test/users?after=a+b%26c&limit=50',
      'https://app.vigia.test/users/..%2F..%2Fevil.example%2Fx%3Fy/deactivate',
    ]);
    await expect(
      transport.request({
        operationId: 'deactivate_users__user_id__deactivate_post',
        kind: 'write',
      }),
    ).rejects.toThrow(/user_id/);
    // `.` y `..` sobrevivirían a encodeURIComponent y URL los resolvería hacia otra operación.
    for (const step_id of ['.', '..']) {
      await expect(
        transport.request({
          operationId: 'close_step_walk_tests__session_id__steps__step_id__close_post',
          kind: 'write',
          path: { session_id: 's1', step_id },
        }),
      ).rejects.toThrow(/step_id/);
    }
    expect(calls).toHaveLength(2);
  });

  it('X-Vigia-Api-Version de cada respuesta llega al observador; unauthenticated y privacy_notice_required, a la sesión', async () => {
    const versions: string[] = [];
    const events: string[] = [];
    server.use(
      variant('me_me_get', () =>
        HttpResponse.json({}, { status: 200, headers: { [API_VERSION_HEADER]: '2026.10.24+abc' } }),
      ),
      variant('sessions_auth_sessions_get', () => {
        const response = apiError('unauthenticated', 401);
        response.headers.set(API_VERSION_HEADER, '2026.10.25');
        return response;
      }),
      variant('list_users_users_get', () => apiError('privacy_notice_required', 403)),
      variant('hierarchy_hierarchy_get', () => apiError('forbidden', 403)),
    );
    const clock = new FakeClock();
    const { transport } = transportWith(clock, {
      onApiVersion: (version) => versions.push(version),
      onSessionEvent: (event) => events.push(event),
    });
    for (const id of [
      'me_me_get',
      'sessions_auth_sessions_get',
      'list_users_users_get',
      'hierarchy_hierarchy_get',
    ] as const) {
      await transport.request({ operationId: id, kind: 'read' }).catch(() => undefined);
    }
    expect(versions).toEqual(['2026.10.24+abc', '2026.10.25']);
    expect(events).toEqual(['unauthenticated', 'privacy_notice_required']);
  });
});

describe('traducción de errores (BR-APP-71, IF-20)', () => {
  it('toda respuesta no 2xx pasa por behaviourFor', async () => {
    server.use(
      variant('invite_users_post', () =>
        HttpResponse.json(
          {
            code: 'conflict',
            detail_code: null,
            message_es: 'El correo ya está invitado.',
            correlation_id: 'c-9',
          },
          { status: 409 },
        ),
      ),
    );
    const clock = new FakeClock();
    const { transport } = transportWith(clock);
    const outcome = await drive(
      clock,
      transport.request({ operationId: 'invite_users_post', kind: 'write', body: {} }),
    );
    const failure = failureOf(outcome);
    expect(failure?.kind).toBe('http');
    if (failure?.kind !== 'http') return;
    expect(failure.error).toMatchObject({ code: 'conflict', status: 409 });
    expect(failure.behaviour).toEqual(behaviourFor(failure.error));
    expect(failure.behaviour.kind).toBe('anchored');
  });

  it('un 502 sin ApiErrorBody (pasarela) es temporarily_unavailable; otro estado sin cuerpo, internal_error', async () => {
    server.use(
      variant('logout_auth_logout_post', () => new HttpResponse('<html>', { status: 502 })),
      variant(
        'enroll_auth_second_factor_enroll_post',
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    const clock = new FakeClock();
    const { transport } = transportWith(clock);
    const gateway = failureOf(
      await drive(
        clock,
        transport.request({ operationId: 'logout_auth_logout_post', kind: 'write' }),
      ),
    );
    expect(gateway).toMatchObject({
      kind: 'http',
      error: { code: 'temporarily_unavailable', status: 502 },
      behaviour: { kind: 'transient', retry_after_seconds: null },
    });
    const internal = failureOf(
      await drive(
        clock,
        transport.request({ operationId: 'enroll_auth_second_factor_enroll_post', kind: 'write' }),
      ),
    );
    expect(internal).toMatchObject({ kind: 'http', behaviour: { kind: 'internal' } });
  });
});

describe('circuitos y reintentos (BR-APP-72 a 74)', () => {
  it('con el circuito abierto, lecturas y escrituras fallan offline sin red', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock);
    const { connection, transport } = transportWith(clock, { fetch });
    for (let i = 0; i < 4; i += 1) {
      connection.register('platform', { ok: false, error: { kind: 'network' } });
    }
    const read = await drive(clock, transport.request({ operationId: 'me_me_get', kind: 'read' }));
    const write = await drive(
      clock,
      transport.request({ operationId: 'logout_auth_logout_post', kind: 'write' }),
    );
    expect([failureOf(read), failureOf(write)]).toEqual([{ kind: 'offline' }, { kind: 'offline' }]);
    expect(calls).toHaveLength(0);
  });

  it('si el circuito se abre durante el retroceso, la lectura no reintenta', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock, ['network']);
    const { connection, transport } = transportWith(clock, { fetch });
    const read = transport.request({ operationId: 'me_me_get', kind: 'read' });
    await flush();
    expect(calls).toHaveLength(1);
    for (let i = 0; i < 3; i += 1) {
      connection.register('platform', { ok: false, error: { kind: 'network' } });
    }
    expect(connection.canRead('platform')).toBe(false);
    expect(failureOf(await drive(clock, read))).toEqual({ kind: 'offline' });
    expect(calls).toHaveLength(1);
  });

  it('half_open: un intento de sonda y ningún reintento si falla', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock, ['network', 'ok']);
    const { connection, transport } = transportWith(clock, { fetch });
    for (let i = 0; i < 4; i += 1) {
      connection.register('platform', { ok: false, error: { kind: 'network' } });
    }
    clock.advance(30_000);
    expect(connection.getSnapshot().circuits.platform.state).toBe('half_open');
    const read = await drive(clock, transport.request({ operationId: 'me_me_get', kind: 'read' }));
    expect(failureOf(read)).toEqual({ kind: 'offline' });
    expect(calls).toHaveLength(1);
  });

  it('una cancelación del llamador corta el retroceso y no cuenta para el circuito', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock, ['network', 'timeout']);
    const { connection, transport } = transportWith(clock, { fetch });
    const controller = new AbortController();
    const read = transport.request({
      operationId: 'me_me_get',
      kind: 'read',
      signal: controller.signal,
    });
    await flush();
    controller.abort();
    expect(failureOf(await drive(clock, read))).toEqual({ kind: 'aborted' });
    expect(calls).toHaveLength(1);
    expect(clock.pendingCount).toBe(0);

    const second = new AbortController();
    const inFlight = transport.request({
      operationId: 'me_me_get',
      kind: 'read',
      signal: second.signal,
    });
    await flush();
    clock.advance(2_000);
    await flush();
    expect(calls).toHaveLength(2);
    second.abort();
    expect(failureOf(await drive(clock, inFlight))).toEqual({ kind: 'aborted' });
    // Solo cuenta el primer fallo de red: la cancelación no se registró.
    expect(connection.getSnapshot().circuits.platform.failures).toHaveLength(1);
  });

  it('una escritura ignora la cancelación del llamador: llega al servidor y da su resultado', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock, ['ok']);
    const { transport } = transportWith(clock, { fetch });
    const controller = new AbortController();
    controller.abort();
    const write = await drive(
      clock,
      transport.request({
        operationId: 'invite_users_post',
        kind: 'write',
        body: { any: 1 },
        signal: controller.signal,
      }),
    );
    expect(write).toEqual({ ok: true, value: { answer: 1 } });
    expect(calls).toHaveLength(1);

    // Cancelar con la escritura en vuelo no la corta: termina por su propio tope de 30 s.
    const slow = scriptedFetch(clock, ['timeout']);
    const inFlight = new AbortController();
    const pending = transportWith(clock, { fetch: slow.fetch }).transport.request({
      operationId: 'invite_users_post',
      kind: 'write',
      body: { any: 1 },
      signal: inFlight.signal,
    });
    const start = clock.now();
    await flush();
    inFlight.abort();
    expect(failureOf(await drive(clock, pending))?.kind).toBe('timeout');
    expect(slow.calls[0]?.endedAt).toBe(start + TIMEOUT_MS.write);
  });

  it('una lectura correcta registra éxito y vuelve a online', async () => {
    const clock = new FakeClock();
    const { fetch } = scriptedFetch(clock, ['network', 'ok']);
    const { connection, transport } = transportWith(clock, { fetch });
    const read = await drive(clock, transport.request({ operationId: 'me_me_get', kind: 'read' }));
    expect(read).toEqual({ ok: true, value: { answer: 2 } });
    expect(connection.getSnapshot().connection.state).toBe('online');
  });

  it('una operación que no es GET no puede declararse lectura', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock);
    const { transport } = transportWith(clock, { fetch });
    await expect(
      transport.request({ operationId: 'logout_auth_logout_post', kind: 'read' }),
    ).rejects.toThrow(TypeError);
    expect(calls).toHaveLength(0);
  });
});

describe('devLog (NFR-APP-42)', () => {
  const MARKERS = {
    body: 'MARCA-CUERPO-7f3a',
    query: 'MARCA-CONSULTA-91c2',
    path: 'MARCA-RUTA-5d0e',
    concession: 'MARCA-CONCESION-2b8d',
    response: 'MARCA-RESPUESTA-c4e1',
  };

  beforeEach(() => {
    server.use(
      variant('update_profile_users__user_id__patch', () =>
        HttpResponse.json({ echo: MARKERS.response }, { status: 200 }),
      ),
      http.get(mswPath('/users'), () => HttpResponse.json({ items: [MARKERS.response] })),
    );
  });

  async function exercise(clock: FakeClock): Promise<void> {
    const transport = createTransport({
      clock,
      connection: createPortConnection(clock),
      concession: () => MARKERS.concession,
    });
    await transport.request({
      operationId: 'update_profile_users__user_id__patch',
      kind: 'write',
      path: { user_id: MARKERS.path },
      body: { display_name: MARKERS.body },
    });
    await transport.request({
      operationId: 'list_users_users_get',
      kind: 'read',
      query: { after: MARKERS.query },
    });
  }

  it('en desarrollo registra método, plantilla, código y duración; nunca cuerpo, cabeceras ni consulta', async () => {
    vi.stubEnv('DEV', true);
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => undefined);
    await exercise(new FakeClock());
    const output = debug.mock.calls.flat().map(String);
    expect(output).toEqual([
      '[api] PATCH /users/{user_id} 200 ok 0 ms',
      '[api] GET /users 200 ok 0 ms',
    ]);
    for (const marker of Object.values(MARKERS)) {
      expect(output.join('\n')).not.toContain(marker);
    }
  });

  it('en producción (DEV falso) no escribe nada', async () => {
    vi.stubEnv('DEV', false);
    const spies = (['debug', 'log', 'info', 'warn', 'error'] as const).map((name) =>
      vi.spyOn(console, name).mockImplementation(() => undefined),
    );
    await exercise(new FakeClock());
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });

  it('un código fuera de la forma cerrada se registra como «other»', () => {
    const lines: string[] = [];
    createDevLog(
      { debug: (line) => lines.push(line) },
      () => true,
    )({
      method: 'get',
      route: '/me',
      status: 500,
      code: 'Bearer abc.def',
      durationMs: 12.6,
    });
    expect(lines).toEqual(['[api] GET /me 500 other 13 ms']);
  });
});

describe('defineScreenPort (LC-APP-14, BR-APP-63, 64)', () => {
  const port = defineScreenPort('SCR-01', {
    listUsers: {
      kind: 'read',
      status: 'fixed',
      operationId: 'list_users_users_get',
      permission: 'users.read',
    },
    invite: {
      kind: 'write',
      status: 'fixed',
      operationId: 'invite_users_post',
      permission: 'users.invite',
    },
    futureRead: {
      kind: 'read',
      status: 'assumed',
      operation: 'GET /zones/{id}/future',
      permission: 'zones.read',
    },
    futureWrite: {
      kind: 'write',
      status: 'assumed',
      operation: 'POST /zones/{id}/future',
      permission: 'zones.write',
    },
  });

  it('tipos derivados del cliente generado', () => {
    expectTypeOf<Awaited<ReturnType<typeof port.listUsers.read>>>().toEqualTypeOf<
      components['schemas']['UsersPage']
    >();
    expectTypeOf<Parameters<typeof port.invite.write>[0]['body']>().toEqualTypeOf<
      components['schemas']['InviteBody']
    >();
    type ListQuery = NonNullable<NonNullable<Parameters<typeof port.listUsers.read>[0]>['query']>;
    expectTypeOf<ListQuery>().toEqualTypeOf<{ after?: string | null; limit?: number }>();
    defineScreenPort('SCR-99', {
      // @ts-expect-error: una lectura solo puede nombrar una operación GET.
      bad: { kind: 'read', status: 'fixed', operationId: 'invite_users_post', permission: 'x' },
    });
  });

  it('declara screen_port_status y la operación de la API de cada operación', () => {
    expect(port.screen).toBe('SCR-01');
    expect(port.operations).toEqual([
      {
        name: 'listUsers',
        kind: 'read',
        status: 'fixed',
        api_operation_id: 'list_users_users_get',
        permission: 'users.read',
      },
      {
        name: 'invite',
        kind: 'write',
        status: 'fixed',
        api_operation_id: 'invite_users_post',
        permission: 'users.invite',
      },
      {
        name: 'futureRead',
        kind: 'read',
        status: 'assumed',
        api_operation_id: null,
        permission: 'zones.read',
      },
      {
        name: 'futureWrite',
        kind: 'write',
        status: 'assumed',
        api_operation_id: null,
        permission: 'zones.write',
      },
    ]);
  });

  it('una operación assumed responde unavailable sin llamar a la red', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock);
    installTransport(transportWith(clock, { fetch }).transport);
    await expect(port.futureRead.read()).rejects.toEqual(new PortError({ kind: 'unavailable' }));
    await expect(port.futureWrite.write()).rejects.toSatisfy(
      (error) => isPortError(error) && error.failure.kind === 'unavailable',
    );
    expect(calls).toHaveLength(0);
  });

  it('valida el cuerpo con validateBody antes de enviar', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock);
    installTransport(transportWith(clock, { fetch }).transport);
    const invalid = { body: { email: 'no-es-correo', extra: 1 } } as unknown as Parameters<
      typeof port.invite.write
    >[0];
    const outcome = await drive(clock, port.invite.write(invalid));
    const failure = failureOf(outcome);
    expect(failure?.kind).toBe('invalid_input');
    // El cuerpo es obligatorio en toda operación con cuerpo: omitirlo también es invalid_input.
    const missing = {} as unknown as Parameters<typeof port.invite.write>[0];
    expect(failureOf(await drive(clock, port.invite.write(missing)))?.kind).toBe('invalid_input');
    expect(calls).toHaveLength(0);
  });

  it('read(params, signal) llega al transporte con su consulta y su señal', async () => {
    const clock = new FakeClock();
    const { calls, fetch } = scriptedFetch(clock);
    installTransport(transportWith(clock, { fetch, origin: 'https://app.vigia.test' }).transport);
    const controller = new AbortController();
    await expect(port.listUsers.read({ query: { limit: 20 } }, controller.signal)).resolves.toEqual(
      {
        answer: 1,
      },
    );
    expect(calls[0]?.url).toBe('https://app.vigia.test/users?limit=20');
    expect(calls[0]?.init?.method).toBe('GET');
  });

  it('sin transporte instalado una operación fija falla, y nombres reservados se rechazan', async () => {
    await expect(port.listUsers.read()).rejects.toThrow(/installTransport/);
    expect(() =>
      defineScreenPort('SCR-02', {
        screen: { kind: 'read', status: 'fixed', operationId: 'me_me_get', permission: 'x' },
      } as never),
    ).toThrow(/reservado/);
  });

  it('slowSince: null antes de 1 s; el instante del cruce desde 1 s; null al terminar', async () => {
    const clock = new FakeClock(5_000);
    const { fetch } = silentFetch(clock);
    installTransport(transportWith(clock, { fetch }).transport);
    const notified: (number | null)[] = [];
    const unsubscribe = port.subscribeSlow(() => notified.push(port.slowSince()));
    const controller = new AbortController();
    const read = port.listUsers.read({}, controller.signal).catch(() => undefined);
    await flush();
    clock.advance(SLOW_AFTER_MS - 1);
    expect(port.slowSince()).toBeNull();
    clock.advance(1);
    expect(port.slowSince()).toBe(5_000 + SLOW_AFTER_MS);
    controller.abort();
    await drive(clock, read);
    expect(port.slowSince()).toBeNull();
    expect(notified).toEqual([5_000 + SLOW_AFTER_MS, null]);
    unsubscribe();
  });
});
