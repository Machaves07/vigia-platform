#!/usr/bin/env node
/**
 * Órdenes de `package.json` que existen desde el andamiaje (VIG-277) pero que completa una tarea
 * posterior: fallan siempre y nombran esa tarea. Uso: `pending.mjs <orden> <VIG-n> <TASK-nnn>`.
 */
const [script = '?', issue = '?', task = '?'] = process.argv.slice(2);
process.stderr.write(
  `«npm run ${script}» todavía no está disponible: lo completa ${issue} (${task}).\n`,
);
process.exit(1);
