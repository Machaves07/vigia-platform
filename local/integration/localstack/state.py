"""Estado de LocalStack comunitario para el entorno de integración (TASK-545, VIG-393; A-76 D3).

LocalStack comunitario guarda todo en memoria: al reiniciar el contenedor pierde los depósitos de
S3, los secretos de Secrets Manager y la clave KMS ``vigia-secrets``. Eso rompería más que las
credenciales de nodo: sin la clave KMS nadie descifra el segundo factor de las personas, y sin los
secretos de firma ya no verifica el expediente. La persistencia nativa (``PERSISTENCE=1``) es de
pago y la edición comunitaria la ignora (comprobado con la 4.14.0). Esta es la forma más simple de
cumplirlo sin pago ni AWS real, dentro del propio contenedor:

- ``restore`` (gancho ``ready.d``, al arrancar) recrea lo que falte: los depósitos (versionados) y
  el centinela de salud; la clave KMS con **el mismo identificador y el mismo material** (etiquetas
  ``_custom_id_`` y ``_custom_key_material_``, que LocalStack admite), de modo que el texto cifrado
  anterior se sigue descifrando; y los secretos y los objetos pequeños de ``vigia-edge``
  (``ca/root.pem``) de la última copia. Los secretos derivados de ``/state`` (la base y la clave del
  hash de origen del alta) se crean si ninguna copia los trae. Al terminar deja ``MARKER``: el
  healthcheck del contenedor no da sano hasta entonces.
- ``save`` (cada ``VIGIA_SAVE_SECONDS``, 10 por omisión, desde ``ready.d``; y a mano) escribe la
  copia en ``/state/localstack-snapshot.json`` (``0600``, atómica, sin reescribir si nada cambió).
  **No guarda nada sin ``MARKER``**: un arranque cuyo ``restore`` falló no puede sustituir una buena
  copia por otra vacía. No hay gancho ``shutdown.d``: LocalStack ya no atiende la API cuando lo
  ejecuta (probado: 503 en ``ListSecrets``).

Qué se pierde: los objetos de los depósitos que no son ``vigia-edge`` (clips, evidencias,
archivo) y las versiones anteriores de un secreto. También lo escrito en los
``VIGIA_SAVE_SECONDS`` anteriores a un reinicio o una caída; ``bootstrap.sh`` y ``admin.sh``
fuerzan un ``save`` al terminar, y los secretos solo cambian con una orden administrativa
(``rotate-key``) o un ``bootstrap``. La clave de la autoridad de nodos no pasa por aquí: vive en su
propio volumen (``VIGIA_NODE_CA_KEY_FILE``).

Corre dentro del contenedor de LocalStack (``/usr/local/bin/python3`` con ``boto3``). Ningún
valor de secreto sale por la salida estándar: solo nombres y recuentos.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ENDPOINT: Final = os.environ.get("VIGIA_LOCALSTACK_ENDPOINT", "http://localhost:4566")
STATE: Final = Path(os.environ.get("VIGIA_STATE_DIR", "/state"))
SNAPSHOT: Final = STATE / "localstack-snapshot.json"
MARKER: Final = Path(os.environ.get("VIGIA_RESTORE_MARKER", "/tmp/vigia-restored"))  # noqa: S108
MAX_OBJECT_BYTES: Final = 1024 * 1024
SENTINEL_KEY: Final = "health/ready-sentinel"
FORMAT_VERSION: Final = 1
_CONFIG: Final = Config(
    connect_timeout=5, read_timeout=15, retries={"max_attempts": 3, "mode": "standard"}
)


def _client(service: str) -> Any:
    return boto3.client(
        service,
        endpoint_url=ENDPOINT,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - credencial fija de LocalStack, no un secreto
        config=_CONFIG,
    )


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"falta la variable {name}")
    return value


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _read_state_file(name: str) -> str:
    return (STATE / name).read_text(encoding="utf-8").strip()


# --- Restauración ---------------------------------------------------------------------------


def ensure_buckets(s3: Any, buckets: list[str], evidence: str) -> None:
    for name in buckets:
        try:
            s3.create_bucket(Bucket=name)
        except ClientError as error:
            if _error_code(error) not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
        s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    try:
        s3.head_object(Bucket=evidence, Key=SENTINEL_KEY)
    except ClientError as error:
        if _error_code(error) not in {"404", "NoSuchKey", "NotFound"}:
            raise
        s3.put_object(Bucket=evidence, Key=SENTINEL_KEY, Body=b"centinela")


def ensure_alias(kms: Any, alias: str, key_id: str) -> None:
    """``alias`` apunta a ``key_id`` (la plataforma nombra la clave por su alias)."""
    try:
        kms.create_alias(AliasName=alias, TargetKeyId=key_id)
    except ClientError as error:
        if _error_code(error) != "AlreadyExistsException":
            raise


def ensure_key(kms: Any, key_id: str, material_file: Path, alias: str) -> None:
    """La clave ``vigia-secrets`` con su identificador y su material de siempre, y su alias."""
    try:
        kms.describe_key(KeyId=key_id)
    except ClientError as error:
        if _error_code(error) != "NotFoundException":
            raise
    else:
        ensure_alias(kms, alias, key_id)
        return
    material = material_file.read_text(encoding="ascii").strip()
    if len(base64.b64decode(material, validate=True)) != 32:
        raise SystemExit("el material de la clave KMS debe tener 32 bytes")
    kms.create_key(
        Description="vigia-secrets de integración",
        Tags=[
            {"TagKey": "_custom_id_", "TagValue": key_id},
            {"TagKey": "_custom_key_material_", "TagValue": material},
        ],
    )
    kms.describe_key(KeyId=key_id)
    ensure_alias(kms, alias, key_id)


def _put_secret(manager: Any, entry: dict[str, Any]) -> None:
    """Recrea el secreto con **el mismo ARN y la misma versión**.

    La base guarda el ARN de cada secreto de firma (con su sufijo aleatorio de seis caracteres) y
    ``SecretsManager`` lo resuelve por ARN: sin el sufijo original, ``vigia-api`` no encuentra las
    claves. LocalStack fija el sufijo con la etiqueta ``_custom_id_`` y el identificador de versión
    con ``ClientRequestToken``.
    """
    value: dict[str, Any] = (
        {"SecretString": entry["string"]}
        if entry.get("string") is not None
        else {"SecretBinary": base64.b64decode(entry["binary"])}
    )
    identity: dict[str, Any] = {}
    if entry.get("arn"):
        identity["Tags"] = [{"Key": "_custom_id_", "Value": str(entry["arn"]).rsplit("-", 1)[-1]}]
    if entry.get("version_id"):
        identity["ClientRequestToken"] = entry["version_id"]
    try:
        manager.create_secret(Name=entry["name"], **value, **identity)
    except ClientError as error:
        if _error_code(error) != "ResourceExistsException":
            raise
        manager.put_secret_value(SecretId=entry["name"], **value)


def _secret_exists(manager: Any, name: str) -> bool:
    try:
        manager.describe_secret(SecretId=name)
    except ClientError as error:
        if _error_code(error) != "ResourceNotFoundException":
            raise
        return False
    return True


def load_snapshot() -> dict[str, Any]:
    if not SNAPSHOT.exists():
        return {"version": FORMAT_VERSION, "secrets": [], "objects": []}
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if document.get("version") != FORMAT_VERSION:
        raise SystemExit("la copia de LocalStack tiene un formato que no conozco")
    return document  # type: ignore[no-any-return]


def restore() -> int:
    buckets = _setting("VIGIA_BUCKETS").split()
    evidence, edge = _setting("VIGIA_EVIDENCE_BUCKET"), _setting("VIGIA_EDGE_BUCKET")
    s3, kms, manager = _client("s3"), _client("kms"), _client("secretsmanager")
    ensure_buckets(s3, buckets, evidence)
    ensure_key(
        kms,
        _read_state_file("kms-secrets.id"),
        STATE / "kms-secrets.material",
        _setting("VIGIA_KMS_KEY_ALIAS"),
    )
    snapshot = load_snapshot()
    for entry in snapshot["secrets"]:
        _put_secret(manager, entry)
    for item in snapshot["objects"]:
        s3.put_object(
            Bucket=edge,
            Key=item["key"],
            Body=base64.b64decode(item["body"]),
            ContentType=item["content_type"],
        )
    derived = {
        _setting("VIGIA_DB_SECRET_NAME"): {
            "string": json.dumps(
                {
                    "engine": "postgres",
                    "host": "postgres",
                    "port": 5432,
                    "dbname": "vigia",
                    "username": "vigia_app",
                    "password": _read_state_file("app.pw"),
                },
                sort_keys=True,
            )
        },
        _setting("VIGIA_ENROLLMENT_SECRET_NAME"): {
            "binary": _read_state_file("enrollment-source.key")
        },
    }
    created = 0
    for name, value in derived.items():
        if not _secret_exists(manager, name):
            _put_secret(manager, {"name": name, **value})
            created += 1
    MARKER.write_text("restaurado\n", encoding="ascii")
    print(
        f"restore: {len(snapshot['secrets'])} secretos y {len(snapshot['objects'])} objetos de la"
        f" copia, {created} secretos derivados"
    )
    return 0


# --- Copia ----------------------------------------------------------------------------------


def collect(manager: Any, s3: Any, edge: str) -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    for page in manager.get_paginator("list_secrets").paginate():
        for item in page["SecretList"]:
            if "DeletedDate" in item:
                continue
            try:
                got = manager.get_secret_value(SecretId=item["Name"])
            except ClientError as error:
                if _error_code(error) == "ResourceNotFoundException":
                    continue
                raise
            entry: dict[str, Any] = {
                "name": item["Name"],
                "arn": got["ARN"],
                "version_id": got["VersionId"],
            }
            if "SecretString" in got:
                entry["string"] = got["SecretString"]
            else:
                entry["binary"] = base64.b64encode(got["SecretBinary"]).decode("ascii")
            found.append(entry)
    objects: list[dict[str, Any]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=edge):
        for item in page.get("Contents", []):
            if item["Size"] > MAX_OBJECT_BYTES:
                continue
            got = s3.get_object(Bucket=edge, Key=item["Key"])
            objects.append(
                {
                    "key": item["Key"],
                    "content_type": got.get("ContentType", "application/octet-stream"),
                    "body": base64.b64encode(got["Body"].read()).decode("ascii"),
                }
            )
    found.sort(key=lambda entry: entry["name"])
    objects.sort(key=lambda entry: entry["key"])
    return {"version": FORMAT_VERSION, "secrets": found, "objects": objects}


def write_atomically(path: Path, text: str) -> bool:
    """Escribe ``text`` en ``path`` (``0600``, entero o nada). ``False`` si ya era igual."""
    with contextlib.suppress(FileNotFoundError):
        if path.read_text(encoding="utf-8") == text:
            return False
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return True


def save() -> int:
    if not MARKER.exists():
        print("save: sin restore terminado; no se escribe nada", file=sys.stderr)
        return 3
    snapshot = collect(_client("secretsmanager"), _client("s3"), _setting("VIGIA_EDGE_BUCKET"))
    changed = write_atomically(SNAPSHOT, json.dumps(snapshot, sort_keys=True))
    print(
        f"save: {len(snapshot['secrets'])} secretos y {len(snapshot['objects'])} objetos"
        f" ({'escrita' if changed else 'sin cambios'})"
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"restore", "save"}:
        print("uso: state.py restore|save", file=sys.stderr)
        return 2
    return restore() if argv[1] == "restore" else save()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
