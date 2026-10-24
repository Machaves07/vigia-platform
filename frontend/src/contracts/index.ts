/**
 * Re-exportaciones tipadas de `@vigia/contracts` (LC-APP-22, NFR-APP-28 y 52).
 *
 * Tipos de todas las entidades, los validadores Ajv compilados (`validate<Entidad>`), las
 * etiquetas de `labels.es.json` (`labels`, `labelEs`), `isCompatible` (comprobada en el arranque
 * contra la etiqueta de contrato que anuncia `GET /me`, VIG-289) y `canonicalize` (no usado en la
 * interfaz). Ninguna copia de esquemas: todo sale del paquete fijado por etiqueta git (ADR-004).
 */
export * from '@vigia/contracts';
export { labelsEs as labels } from '@vigia/contracts';
