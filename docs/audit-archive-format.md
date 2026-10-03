# Archivo de una partición de auditoría (`vigia-audit-archive`, versión 1)

Lo escribe la tarea mensual `archive_audit_partitions` de `vigia-worker`
(`backend/src/vigia_platform/shared/archive/audit_archive.py`, TASK-131, LC-NUC-33,
PAT-NUC-MAN-03). La auditoría vive 24 meses en línea (NFR-NUC-32); después, cada partición mensual
de `shared.audit_entry` se exporta a este archivo, se comprueba leyéndolo de vuelta del almacén y
solo entonces se desprende de la base. El procedimiento de investigación completo es el runbook
6.5 (TASK-152); este documento describe el archivo y la orden de solo lectura que ese runbook usa.

## 1. Dónde está

- Depósito `vigia-archive` del entorno (bloqueo de objetos en modo cumplimiento, 10 años), cifrado
  `aws:kms` con la clave `vigia-archive`.
- Clave del objeto: `audit/<AAAA-MM>/audit_entry_<AAAA>_<MM>.zip`.
- El registro `audit_partition_archived` de la cadena de expediente de la **organización
  proveedora** lleva `partition_name`, `period`, `archive_object_key`, `archive_sha256` (SHA-256
  del objeto, en hexadecimal), `entry_count` y `archived_at`. Es la referencia de la restauración:
  un objeto con otro SHA-256 no se acepta.

## 2. Contenido del ZIP

```text
archive.json                                   manifiesto del archivo (§3)
vigia_verify.py                                copia del verificador de paquetes
organizations/<organization_id>/manifest.json  paquete vigia-package (docs/package-format.md)
organizations/<organization_id>/chains/audit.jsonl
```

Una organización por cada una que tenga entradas en la partición. Cada directorio
`organizations/<organization_id>/` es un paquete `vigia-package` con **una** cadena de auditoría:
el tramo de la cadena de esa organización que cae en el mes, desde su primera secuencia
(`first_sequence` mayor que 1 salvo que el tramo empiece en la génesis), con las claves públicas
`checkpoint` vigentes y retiradas. Cada línea es la entrada de auditoría en la forma del paquete,
con `filters` copiado byte a byte de la columna. Ningún otro miembro se acepta al leer.

## 3. `archive.json`

| Campo | Contenido |
|---|---|
| `format`, `format_version` | `"vigia-audit-archive"` y `1` |
| `partition`, `period` | `audit_entry_AAAA_MM` y `AAAA-MM` |
| `exported_at` | Marca UTC con milisegundos de la exportación |
| `entry_count` | Entradas de la partición, todas las organizaciones |
| `verifier`, `verifier_sha256` | `vigia_verify.py` y su SHA-256 |
| `organizations` | Por organización: `organization_id`, `package`, `entries`, `first_sequence`, `last_sequence`, `first_previous_hash` (enlace con la entrada anterior, en otra partición), `last_hash`, `checkpoints` (cuántos hay en el tramo) y `last_checkpoint` (el último punto de control del tramo: `sequence`, `entry_id`, `entry_hash`, `covered_sequence`, `covered_hash`, `key_id`, `taken_at`; `null` si el tramo no tiene ninguno) |

## 4. Qué se comprueba antes de desprender

Sobre los bytes **leídos de vuelta** del almacén: el SHA-256 es el de lo subido; los miembros son
exactamente los declarados; cada cadena está íntegra con `chain_walk` (enlaces, hashes y firma de
cada punto de control con las claves publicadas); recuentos y puntos de control son los
declarados; cada entrada, devuelta a columnas, es **igual** a su fila de la base; y el verificador
incluido es el esperado. Después, en una transacción, `shared.vigia_detach_audit_partition`
vuelve a contar las entradas bajo bloqueo exclusivo, desprende la partición (la tabla queda en la
base, de solo anexar) y se escribe `audit_partition_archived`. Si algo falla, la partición sigue
adjunta, no hay registro y se alerta (`security_alert` con `alert_kind =
audit_archive_verification_failed` y la entrada de auditoría `integrity_verification` con
resultado `error`, en la proveedora).

## 5. Restauración de solo lectura: `vigia-admin restore-audit-partition`

La orden la añade `vigia-admin` (TASK-132) sobre `restore_audit_partition` y `extract_archive`:

```text
vigia-admin restore-audit-partition audit/2026-10/audit_entry_2026_10.zip \
    --sha256 <archive_sha256 del registro audit_partition_archived> --output <directorio nuevo>
```

1. Descarga el objeto de `vigia-archive` (el rol de la tarea solo tiene `GetObject`) y exige el
   SHA-256 del registro.
2. Verifica cada cadena con las claves del propio archivo, sin acceso a la base.
3. Escribe los paquetes y `vigia_verify.py` en `--output`, que no debe existir. Nada sale de ese
   directorio.
4. **No escribe en la base.** Para investigar, cada paquete se comprueba también con el verificador
   incluido, igual que un cliente: `python vigia_verify.py organizations/<organization_id>`.
   Volver a cargar las filas en una base de investigación es un paso del runbook 6.5, nunca sobre
   la base de producción.

Las filas restauradas (`RestoredPartition.rows`) tienen exactamente las columnas de
`shared.audit_entry` salvo `filters_json`, que es generada (PR-NUC-56: `restore(export(p)) = p`).
