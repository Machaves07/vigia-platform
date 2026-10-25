import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';

import { backendPath } from './world';

let child: ChildProcessWithoutNullStreams | undefined;
let exited: Promise<number | null> | undefined;

/** EOF solicita liberación normal, incluso si globalSetup no llegó a terminar. */
export async function stopWorld(): Promise<void> {
  if (!child || !exited) return;
  const process = child;
  child = undefined;
  process.stdin.end();
  const killTimer = setTimeout(() => process.kill('SIGTERM'), 120_000);
  try {
    const code = await exited;
    if (code !== 0) throw new Error('El mundo no terminó limpiamente');
  } finally {
    clearTimeout(killTimer);
  }
}

export async function startWorld(): Promise<void> {
  child = spawn('uv', ['run', '--frozen', 'python', '-m', 'tests.e2e_u05'], {
    cwd: backendPath,
    stdio: 'pipe',
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  });
  const processChild = child;
  exited = new Promise((resolve, reject) => {
    processChild.once('error', reject);
    processChild.once('close', resolve);
  });
  try {
    await new Promise<void>((resolve, reject) => {
      // Migraciones, descarga inicial de imágenes y la precondición temporal de U-04.
      const timer = setTimeout(() => {
        reject(new Error('El mundo no arrancó en 15 minutos'));
      }, 900_000);
      let output = '';
      processChild.stdout.on('data', (chunk: Buffer) => {
        output = (output + chunk.toString()).slice(-4096);
        const failure = /VIGIA_WORLD_FAILURE ([^\r\n]+)/.exec(output)?.[1];
        if (failure !== undefined) {
          clearTimeout(timer);
          reject(new Error(`Fallo del mundo: ${failure}`));
        }
        if (output.includes('VIGIA_WORLD_READY')) {
          clearTimeout(timer);
          resolve();
        }
      });
      // Drenar sin publicar: ni errores de librerías ni sus cuerpos llegan al informe de UI.
      processChild.stderr.resume();
      void exited?.then(
        () => {
          clearTimeout(timer);
          reject(new Error('El mundo terminó antes de estar listo'));
        },
        (error: unknown) => {
          clearTimeout(timer);
          reject(error instanceof Error ? error : new Error('No se pudo arrancar uv'));
        },
      );
    });
  } catch (error) {
    // La limpieza no debe ocultar el diagnóstico seguro del arranque.
    await stopWorld().catch(() => undefined);
    throw error;
  }
}
