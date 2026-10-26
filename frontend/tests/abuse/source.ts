/** Recorrido estático del árbol completo, incluidos archivos nuevos fuera de manifiestos. */
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import ts from 'typescript';

const ROOT = join(import.meta.dirname, '../../src');

export function styles(root = ROOT): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name);
    return entry.isDirectory()
      ? styles(path)
      : entry.name.endsWith('.css')
        ? [readFileSync(path, 'utf8')]
        : [];
  });
}

export function sources(root = ROOT): { path: string; source: ts.SourceFile }[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name);
    if (entry.isDirectory()) return sources(path);
    if (!/\.[cm]?[jt]sx?$/.test(entry.name)) return [];
    return [
      {
        path: relative(ROOT, path).replaceAll('\\', '/'),
        source: ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true),
      },
    ];
  });
}

export function nodes(source: ts.Node): ts.Node[] {
  const result: ts.Node[] = [source];
  source.forEachChild((child) => {
    result.push(...nodes(child));
  });
  return result;
}
