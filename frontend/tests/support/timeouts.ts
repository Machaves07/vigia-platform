/**
 * Tope de las pruebas de propiedades sobre generadores y manejadores (VIG-286). No es lo que se
 * prueba: con `FC_PROFILE=nightly` cada propiedad corre 2 000 ejemplos y la suite comparte la
 * máquina con otras, así que el tope es holgado (AGENTS.md: nada de topes cortos «porque basta»).
 */
export const PROPERTY_TIMEOUT_MS = 300_000;
