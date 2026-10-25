#!/usr/bin/env node
/**
 * VIG-345: descarga la compilación de WebKit de `tests/browser/webkit-build.json` (revisión
 * compatible con la CSP estricta), comprueba su SHA-256 antes de descomprimirla y la deja en
 * `VIGIA_WEBKIT_DIR` o `~/.cache/vigia/webkit-<revisión>`, el mismo directorio que resuelve
 * `tests/browser/webkit.ts`. No toca la caché de Playwright ni instala bibliotecas del sistema
 * (`npx playwright install-deps webkit` o la imagen oficial de Playwright).
 *
 * Uso: `node scripts/install_webkit.mjs`. `VIGIA_WEBKIT_PLATFORM` fuerza la clave de descarga
 * (`ubuntu22.04-x64`, `ubuntu24.04-x64`) en anfitriones compatibles que no se identifican así.
 */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createWriteStream, existsSync, readFileSync } from 'node:fs';
import { mkdir, mkdtemp, rename, rm, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

import build from '../tests/browser/webkit-build.json' with { type: 'json' };

const MARKER = 'INSTALLATION_COMPLETE';

function hostPlatform() {
  const forced = process.env.VIGIA_WEBKIT_PLATFORM;
  if (forced) return forced;
  if (process.platform !== 'linux' || process.arch !== 'x64') {
    return `${process.platform}-${process.arch}`;
  }
  const release = existsSync('/etc/os-release') ? readFileSync('/etc/os-release', 'utf8') : '';
  const field = (/** @type {string} */ name) =>
    new RegExp(`^${name}="?([^"\\n]*)"?$`, 'm').exec(release)?.[1] ?? '';
  return `${field('ID')}${field('VERSION_ID')}-x64`;
}

/**
 * @param {string} path ruta relativa al espejo
 * @param {string} sha256 resumen esperado, en hexadecimal
 * @param {string} target archivo de destino
 */
async function download(path, sha256, target) {
  for (const mirror of build.mirrors) {
    const url = `${mirror}/${path}`;
    const response = await fetch(url).catch(() => null);
    if (response === null || !response.ok || response.body === null) {
      process.stderr.write(`No se pudo descargar ${url}; se prueba el siguiente espejo.\n`);
      continue;
    }
    const hash = createHash('sha256');
    const digest = new Transform({
      transform(chunk, _encoding, done) {
        hash.update(chunk);
        done(null, chunk);
      },
    });
    const body = /** @type {import('node:stream/web').ReadableStream} */ (response.body);
    await pipeline(Readable.fromWeb(body), digest, createWriteStream(target));
    const actual = hash.digest('hex');
    if (actual === sha256) return;
    // Un espejo que sirve otro contenido no se usa: nunca se descomprime un archivo sin verificar.
    throw new Error(`SHA-256 inesperado en ${url}: ${actual}, se esperaba ${sha256}`);
  }
  throw new Error(`Ningún espejo sirvió ${path}`);
}

const platform = hostPlatform();
/** @type {Record<string, { path: string, sha256: string } | undefined>} */
const downloads = build.downloads;
const entry = downloads[platform];
if (entry === undefined) {
  process.stderr.write(
    `No hay compilación WebKit ${build.revision} fijada para «${platform}». ` +
      `Plataformas: ${Object.keys(build.downloads).join(', ')}.\n`,
  );
  process.exit(1);
}
// Misma regla que `compatibleWebkitDir()` de `tests/browser/webkit.ts`.
const dir =
  process.env.VIGIA_WEBKIT_DIR || join(homedir(), '.cache', 'vigia', `webkit-${build.revision}`);
const marker = join(dir, MARKER);
if (existsSync(marker) && readFileSync(marker, 'utf8').trim() === entry.sha256) {
  process.stdout.write(`WebKit ${build.revision} ya está instalado en ${dir}\n`);
  process.exit(0);
}
await mkdir(dirname(dir), { recursive: true });
const work = await mkdtemp(join(dirname(dir), `.webkit-${build.revision}-`));
try {
  const zip = join(work, 'webkit.zip');
  await download(entry.path, entry.sha256, zip);
  const tree = join(work, 'tree');
  execFileSync('unzip', ['-q', zip, '-d', tree], { stdio: 'inherit' });
  await writeFile(join(tree, MARKER), `${entry.sha256}\n`);
  await rm(dir, { recursive: true, force: true });
  await rename(tree, dir);
} finally {
  await rm(work, { recursive: true, force: true });
}
process.stdout.write(
  `WebKit ${build.revision} (${build.browserVersion}, ${platform}) instalado en ${dir}\n`,
);
