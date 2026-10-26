/**
 * Intermediario de fallos del mundo (VIG-334): entre el balanceador de la aplicación y la API vive
 * el `FaultProxy` de U-02 (`backend/tests/fault_proxy.py`); su canal local cambia el modo en
 * caliente. `forward` reenvía, `freeze` retiene sin responder (la API «no responde») y `refuse`
 * corta las conexiones.
 */
import type { World } from './world';

export type FaultMode = 'forward' | 'freeze' | 'refuse';

export async function setPlatformFault(world: World, mode: FaultMode): Promise<void> {
  const response = await fetch(`${world.fault_control_url}/mode/${mode}`, { method: 'POST' });
  if (response.status !== 204) {
    throw new Error(`El canal de fallos rechazó el modo ${mode} (${String(response.status)}).`);
  }
}

/**
 * Fin de una caída `freeze`: primero corta las conexiones que el intermediario retuvo (con sus
 * peticiones ya abandonadas en cola) y después reenvía. Sin el corte, la reserva de conexiones del
 * balanceador recibía de golpe esa cola y retrasaba la primera carga de la prueba siguiente.
 */
/**
 * Saca de la ventana del límite de exportación (5 por usuario y hora, PAT-LAZ-ESC-02) las
 * solicitudes ya hechas en el mundo, como si hubiera pasado la hora (VIG-356). Los seis proyectos
 * comparten mundo y persona coordinadora: sin esto, FS-APP-01 y 06 recibían 429 a partir del
 * tercer proyecto. El límite de producción no cambia.
 */
export async function rewindExportWindow(world: World): Promise<void> {
  const response = await fetch(`${world.fault_control_url}/exports/rewind`, { method: 'POST' });
  if (response.status !== 204) {
    throw new Error(`El mundo no reinició la ventana de exportación (${String(response.status)}).`);
  }
}

export async function restorePlatform(world: World): Promise<void> {
  await setPlatformFault(world, 'refuse');
  await setPlatformFault(world, 'forward');
}
