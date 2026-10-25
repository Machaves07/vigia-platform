import { describe, expect, it } from 'vitest';

import { createConnection, type ConnectionState } from '@/state/connection';
import {
  DEPENDENCIES,
  FAILURE_WINDOW_MS,
  initialConnection,
  reduceConnection,
} from '@/state/connection/reducer';

import { fixture } from '../../support/connection';
import { FakeClock } from '../../support/fakeClock';

describe('conexión con reloj inyectado', () => {
  it('agota los reintentos de 2, 4 y 8 s antes de offline (U05-H-10)', () => {
    const { clock, connection, fail } = fixture();
    for (const delay of [0, 2_000, 4_000]) {
      clock.advance(delay);
      fail();
      expect(connection.getSnapshot().connection.state).toBe('degraded');
      expect(connection.canWrite()).toBe(true);
    }
    clock.advance(8_000);
    fail();
    const state: ConnectionState = connection.getSnapshot().connection;
    expect(state).toEqual({
      state: 'offline',
      since: '1970-01-01T00:00:14.000Z',
      last_success_at: null,
    });
    expect(connection.canRead('platform')).toBe(false);
    expect(connection.canWrite()).toBe(false);
    expect(connection.writeBlockReason()).toBe('sin conexión con la plataforma');
  });

  it('permanece abierto 30 s completos desde el cuarto fallo y pasa a half_open', () => {
    const { clock, connection, open, fail } = fixture();
    open();
    clock.advance(29_999);
    fail(); // Una petición que ya estaba en vuelo no prolonga la apertura.
    expect(connection.canRead('platform')).toBe(false);
    clock.advance(1);
    expect(connection.getSnapshot().circuits.platform.state).toBe('half_open');
    expect(connection.getSnapshot().connection.state).toBe('degraded');
    expect(connection.canRead('platform')).toBe(true);
    fail();
    expect(connection.getSnapshot().circuits.platform.retry_at).toBe(60_000);
    connection.registerProbe('platform', true);
    expect(connection.getSnapshot().connection.state).toBe('online');
    expect(connection.canWrite()).toBe(true);
    expect(connection.writeBlockReason()).toBeNull();
  });

  it('vence la ventana móvil justo a FAILURE_WINDOW_MS sin abrir por fallos dispersos', () => {
    const { clock, connection, fail } = fixture();
    fail();
    clock.advance(FAILURE_WINDOW_MS - 1);
    expect(connection.getSnapshot().circuits.platform.failures).toHaveLength(1);
    clock.advance(1);
    expect(connection.getSnapshot().connection.state).toBe('online');
    for (let i = 0; i < 3; i++) fail();
    expect(connection.getSnapshot().circuits.platform.state).toBe('closed');
    fail();
    expect(connection.getSnapshot().circuits.platform.state).toBe('open');
  });

  it.each(DEPENDENCIES)('aísla %s de las otras dependencias', (dep) => {
    const { connection, open } = fixture();
    open(dep);
    for (const other of DEPENDENCIES) expect(connection.canRead(other)).toBe(other !== dep);
    expect(connection.getSnapshot().connection.state).toBe(
      dep === 'platform' ? 'offline' : 'degraded',
    );
    expect(connection.canWrite()).toBe(dep !== 'platform');
  });

  it('no cuenta límites, 503 de negocio ni almacenamiento de sección contra platform', () => {
    const { connection, fail } = fixture();
    for (const error of [
      'rate_limited',
      'throttled',
      'business_503',
      'storage_unavailable',
    ] as const) {
      for (let i = 0; i < 10; i++) fail('platform', error);
    }
    expect(connection.getSnapshot().connection.state).toBe('online');
    for (let i = 0; i < 4; i++) fail('evidence_store', 'storage_unavailable');
    expect(connection.getSnapshot().connection.state).toBe('degraded');
    expect(connection.canWrite()).toBe(true);
  });

  it('reinicia la cuenta con éxito y conserva since mientras no cambia el estado', () => {
    const { clock, connection, fail } = fixture();
    clock.advance(1_000);
    fail();
    clock.advance(1_000);
    fail();
    expect(connection.getSnapshot().connection.since).toBe('1970-01-01T00:00:01.000Z');
    connection.register('platform', { ok: true });
    expect(connection.getSnapshot().connection.last_success_at).toBe('1970-01-01T00:00:02.000Z');
    for (let i = 0; i < 3; i++) fail();
    expect(connection.canRead('platform')).toBe(true);
  });

  it('es puro, no muta snapshots anteriores y publica solo cambios; desuscribe', () => {
    const initial = initialConnection(0);
    const copy = structuredClone(initial);
    const event = { type: 'register', dependency: 'platform', outcome: 'failure' } as const;
    expect(reduceConnection(initial, event, 1)).toEqual(reduceConnection(initial, event, 1));
    expect(initial).toEqual(copy);
    const { connection, fail } = fixture();
    let changes = 0;
    const unsubscribe = connection.subscribe(() => {
      changes++;
    });
    connection.tick();
    expect(changes).toBe(0);
    fail();
    expect(changes).toBe(1);
    unsubscribe();
    fail();
    expect(changes).toBe(1);
  });

  it('no ejecuta ni guarda escrituras al recuperar conexión (BR-APP-74)', () => {
    const clock = new FakeClock();
    let executions = 0;
    const connection = createConnection<null>({ clock, counts_for_circuit: () => true });
    for (let i = 0; i < 4; i++) connection.register('platform', { ok: false, error: null });
    if (connection.canWrite()) executions++;
    connection.registerProbe('platform', true);
    clock.advance(120_000);
    expect(executions).toBe(0);
  });
});
