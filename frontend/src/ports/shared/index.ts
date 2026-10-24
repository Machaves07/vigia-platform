/**
 * Lo que toda página puede importar de `src/ports/` además de su propio puerto (BR-APP-62,
 * `tests/unit/ports/only_own_port.test.ts`): el error de los puertos. Los puertos compartidos
 * entre pantallas (sesión, VIG-300) viven también en esta carpeta.
 */
export { isPortError, PortError } from '../base';
