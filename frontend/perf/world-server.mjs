#!/usr/bin/env node
/**
 * Servidor de Lighthouse CI (`startServerCommand` de `lighthouserc.json`, NFR-APP-02): arranca el
 * mundo de extremo a extremo de U-05 (`backend/tests/e2e_u05`, el mismo de `npm run e2e`), que
 * sirve `frontend/dist` con la raíz de producción, TLS local y la política de contenido real, y
 * reenvía por TCP un puerto fijo (`127.0.0.1:47330`) al puerto aleatorio del mundo. El reenvío es
 * de bytes: TLS, cabeceras y CSP llegan intactas del servidor real.
 *
 * Imprime `VIGIA_PERF_READY` cuando el puerto fijo acepta conexiones. Al recibir `SIGTERM` o
 * `SIGINT` (Lighthouse CI lo termina al acabar) cierra el canal del mundo, que libera sus
 * contenedores, y sale cuando el mundo terminó.
 *
 * Necesita Docker, `uv` y la construcción en `dist/` (`npm run build`). En el WSL del proyecto,
 * `DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales`, como `npm run e2e`.
 */
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createServer, connect } from 'node:net';
import { fileURLToPath } from 'node:url';

const PERF_HOST = '127.0.0.1';
const PERF_PORT = 47330;

const backend = fileURLToPath(new URL('../../backend', import.meta.url));
const manifest = fileURLToPath(new URL('../tests/e2e/.world.json', import.meta.url));

const world = spawn('uv', ['run', '--frozen', 'python', '-m', 'tests.e2e_u05'], {
  cwd: backend,
  stdio: ['pipe', 'pipe', 'inherit'],
  env: { ...process.env, PYTHONUNBUFFERED: '1' },
});

let stopping = false;
const stop = () => {
  if (stopping) return;
  stopping = true;
  // EOF del canal: el mundo libera sus contenedores y termina (`tests/e2e/lifecycle.ts`).
  world.stdin.end();
  setTimeout(() => world.kill('SIGTERM'), 120_000).unref();
};
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
world.on('close', (code) => {
  process.exit(stopping && code === 0 ? 0 : 1);
});

let output = '';
world.stdout.on('data', (chunk) => {
  output = (output + chunk.toString()).slice(-4096);
  const failure = /VIGIA_WORLD_FAILURE ([^\r\n]+)/.exec(output)?.[1];
  if (failure !== undefined) {
    process.stderr.write(`Fallo del mundo: ${failure}\n`);
    stop();
    return;
  }
  if (!output.includes('VIGIA_WORLD_READY') || stopping) return;
  output = '';
  const target = new URL(JSON.parse(readFileSync(manifest, 'utf8')).base_url);
  const forward = createServer((client) => {
    const upstream = connect(Number(target.port), target.hostname);
    client.pipe(upstream).pipe(client);
    const close = () => {
      client.destroy();
      upstream.destroy();
    };
    client.on('error', close);
    upstream.on('error', close);
  });
  forward.listen(PERF_PORT, PERF_HOST, () => {
    process.stdout.write(
      `VIGIA_PERF_READY https://${target.hostname}:${String(PERF_PORT)}/ -> ${target.port}\n`,
    );
  });
});
