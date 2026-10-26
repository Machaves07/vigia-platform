import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ next: undefined as ChildProcessWithoutNullStreams | undefined }));
vi.mock('node:child_process', () => ({
  // No registrar argumentos: el entorno real puede contener credenciales del orquestador.
  spawn: () => {
    if (state.next === undefined) throw new Error('Falta el proceso sintético');
    return state.next;
  },
}));
vi.mock('../e2e/world', () => ({ backendPath: '/backend-synthetic' }));

import { startWorld, stopWorld } from '../e2e/lifecycle';

let child: ChildProcessWithoutNullStreams;
beforeEach(() => {
  child = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(),
    stdout: new PassThrough(),
    stderr: new PassThrough(),
    kill: () => true,
  }) as unknown as ChildProcessWithoutNullStreams;
  state.next = child;
});
afterEach(async () => {
  await stopWorld().catch(() => undefined);
  state.next = undefined;
});

it('preserva el diagnóstico seguro de Python aunque la limpieza termine con código no cero', async () => {
  const outcome = startWorld().catch((error: unknown) => error);
  child.stdout.emit('data', Buffer.from('VIGIA_WORLD_FAILURE ValueError (arranque:1)\n'));
  child.emit('close', 1, null);
  const error = await outcome;
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toBe('Fallo del mundo: ValueError (arranque:1)');
});

it('clasifica una excepción previa al arranque sin reproducir el contenido de stderr', async () => {
  const payload = ['contenido', 'sintetico', 'reservado'].join('-');
  const outcome = startWorld().catch((error: unknown) => error);
  child.stderr.emit('data', Buffer.from(`ImportError: ${payload}\n`));
  child.emit('close', 1, null);
  const error = await outcome;
  expect((error as Error).message).toBe(
    'El mundo terminó antes de estar listo (código 1; ImportError)',
  );
  expect((error as Error).message).not.toContain(payload);
});

it('distingue una señal de terminación externa del fallo de una prueba funcional', async () => {
  const outcome = startWorld().catch((error: unknown) => error);
  child.emit('close', null, 'SIGKILL');
  expect(((await outcome) as Error).message).toBe('El proceso del mundo terminó por SIGKILL');
});
