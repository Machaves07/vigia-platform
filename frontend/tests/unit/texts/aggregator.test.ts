// @vitest-environment node
/**
 * Agregador del catálogo de textos (LC-APP-21): reúne `src/texts/*.json` y rechaza claves
 * repetidas entre archivos. Las pruebas de repetición escriben archivos temporales reales.
 */
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  hasText,
  loadScreenTexts,
  mergeTextModules,
  screenTextFiles,
  TextCatalogError,
  text,
  texts,
} from '@/texts';

const directories: string[] = [];

/** Lee una carpeta como la lee `import.meta.glob('./*.json')`: ruta relativa → contenido. */
function loadDirectory(files: Readonly<Record<string, unknown>>): Record<string, unknown> {
  const directory = mkdtempSync(join(tmpdir(), 'vigia-texts-'));
  directories.push(directory);
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(directory, name), JSON.stringify(content), 'utf8');
  }
  const modules: Record<string, unknown> = {};
  for (const name of readdirSync(directory).filter((file) => file.endsWith('.json'))) {
    modules[`./${name}`] = JSON.parse(readFileSync(join(directory, name), 'utf8')) as unknown;
  }
  return modules;
}

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true });
});

describe('catálogo de textos', () => {
  it('carga src/texts/common.json con import.meta.glob', () => {
    expect(texts['common.app_name']).toBe('Vigía');
    expect(text('common.loading')).toBe('Cargando la aplicación…');
  });

  it('pasa con un solo archivo y con ninguno', () => {
    const modules = loadDirectory({ 'common.json': { 'common.app_name': 'Vigía' } });
    expect(mergeTextModules(modules)).toEqual({ 'common.app_name': 'Vigía' });
    expect(mergeTextModules(loadDirectory({}))).toEqual({});
  });

  it('reúne archivos de dominios distintos', () => {
    const modules = loadDirectory({
      'common.json': { 'common.app_name': 'Vigía' },
      'loop.json': { 'loop.title': 'Lazo de cierre' },
    });
    expect(mergeTextModules(modules)).toEqual({
      'common.app_name': 'Vigía',
      'loop.title': 'Lazo de cierre',
    });
  });

  it('rechaza dos archivos con la misma clave y nombra ambos', () => {
    const modules = loadDirectory({
      'common.json': { 'common.app_name': 'Vigía' },
      'other.json': { 'common.app_name': 'Otra' },
    });
    expect(() => mergeTextModules(modules)).toThrow(TextCatalogError);
    expect(() => mergeTextModules(modules)).toThrow(
      'Clave «common.app_name» repetida en ./common.json y ./other.json.',
    );
  });

  it.each([
    ['un arreglo', ['common.app_name']],
    ['un texto suelto', 'Vigía'],
    ['null', null],
  ])('rechaza un archivo que es %s', (_label, content) => {
    expect(() => mergeTextModules({ './bad.json': content })).toThrow(TextCatalogError);
  });

  it.each([
    ['sin dominio', { app_name: 'Vigía' }],
    ['con mayúsculas', { 'common.AppName': 'Vigía' }],
    ['que empieza por _', { '__proto__.x': 'Vigía' }],
    ['con texto vacío', { 'common.empty': '   ' }],
    ['con un número', { 'common.count': 3 }],
  ])('rechaza una clave %s', (_label, content) => {
    expect(() => mergeTextModules({ './bad.json': content })).toThrow(TextCatalogError);
  });

  it('una clave desconocida es un error, no un texto vacío', () => {
    expect(() => text('common.missing')).toThrow(TextCatalogError);
  });

  it('el catálogo no se puede modificar desde fuera', () => {
    const writable = texts as Record<string, string>;
    expect(() => {
      writable['common.app_name'] = 'Otra';
    }).toThrow(TypeError);
    expect(() => {
      writable['common.injected'] = 'Otra';
    }).toThrow(TypeError);
    expect(() => {
      delete writable['common.app_name'];
    }).toThrow(TypeError);
    expect(text('common.app_name')).toBe('Vigía');
    expect(hasText('common.injected')).toBe(false);
  });
});

describe('textos por ruta (VIG-364)', () => {
  it('cada pantalla diferida trae sus archivos y solo los suyos', () => {
    expect(screenTextFiles(['SCR-08'])).toEqual(['./scr08.json', './scr08_detail.json']);
    expect(screenTextFiles(['SCR-09'])).toEqual(['./scr09_classify.json', './scr09_closure.json']);
    expect(screenTextFiles(['SCR-04'])).toEqual(['./scr04.json', './scr04_parameters.json']);
    expect(screenTextFiles(['SCR-10', 'SCR-16'])).toEqual(['./scr10.json', './scr16.json']);
  });

  it('los textos del armazón no son diferidos: SCR-01 solo difiere su administración', () => {
    expect(screenTextFiles(['SCR-01'])).toEqual(['./scr01_admin.json', './scr01_hierarchy.json']);
    expect(screenTextFiles(['SCR-15'])).toEqual([]);
  });

  it('todos los archivos de pantalla salvo scr01.json y scr15.json se cargan con su ruta', () => {
    const screens = Array.from(
      { length: 16 },
      (_, index) => `SCR-${String(index + 1).padStart(2, '0')}`,
    );
    const onDisk = readdirSync(join(process.cwd(), 'src/texts'))
      .filter((file) => /^scr\d{2}.*\.json$/.test(file))
      .filter((file) => file !== 'scr01.json' && file !== 'scr15.json')
      .map((file) => `./${file}`)
      .sort();
    expect(screenTextFiles(screens)).toEqual(onDisk);
  });

  it('cargar otra vez los textos de una pantalla no repite claves ni falla', async () => {
    await loadScreenTexts(['SCR-08', 'SCR-09']);
    await loadScreenTexts(['SCR-08']);
    expect(hasText('scr08.home.title')).toBe(true);
  });
});
