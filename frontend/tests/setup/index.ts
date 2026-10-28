/**
 * Preparación común de todas las pruebas de vitest (`vitest.config.ts`, `setupFiles`).
 *
 * El orden importa: fast-check primero, para que cualquier propiedad declarada al cargar un
 * archivo ya use el perfil y la semilla de la corrida.
 */
import './fastCheck';
import './testingLibrary';
import './axe';
import './msw';
import './texts';
