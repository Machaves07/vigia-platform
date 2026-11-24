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
  (``ca/root.pem``) de la última copia. Los secretos derivados de ``/state`` (la base, la clave del
  hash de origen del alta y la del relevo firmado del almacén, TASK-607) se crean si ninguna copia
  los trae. Al terminar deja ``MARKER``: el
  healthcheck del contenedor no da sano hasta entonces.
- ``save`` (cada ``VIGIA_SAVE_SECONDS``, 10 por omisión, desde ``ready.d``; y a mano) escribe la
  copia en ``/state/localstack-snapshot.json`` (``0600``, atómica, sin reescribir si nada cambió).
  **No guarda nada sin ``MARKER``**: un arranque cuyo ``restore`` falló no puede sustituir una buena
  copia por otra vacía. No hay gancho ``shutdown.d``: LocalStack ya no atiende la API cuando lo
  ejecuta (probado: 503 en ``ListSecrets``).

Espejo de ``vigia-evidence`` (TASK-608, A-130): los clips y los adjuntos del cierre viven en el
volumen ``almacen`` (montado solo aquí, en ``/almacen``):

- ``save`` copia, **solo** de las claves nuevas o cambiadas (``ETag`` y tamaño), la versión vigente
  con ``ChecksumMode=ENABLED`` en ``/almacen/objetos/<sha256>`` (``0600``, atómica) tras comprobar
  que su SHA-256 es la del almacén, y escribe ``/almacen/indice.json`` (``0600``, atómico) con la
  suma, el tamaño, el tipo, los metadatos de usuario y el ``ETag`` de cada clave. Las bajas (una
  clave que ya no está en LocalStack sale del índice y su blob, si nadie más lo usa, se borra) solo
  ocurren con la restauración de este arranque terminada (``EVIDENCE_MARKER``). Con el tope
  ``VIGIA_ALMACEN_MAX_BYTES`` (2 GiB) alcanzado no copia nada nuevo, no borra nada guardado y lo
  deja en el registro (``almacen_lleno``). Ni ``vigia-edge``, ni ``vigia-archive``, ni el centinela.
- ``restore``, antes de ``MARKER``, vuelve a subir cada clave del índice que falte con sus bytes,
  ``ChecksumSHA256``, su tipo y sus metadatos; un blob cuya suma no es la del índice no se sube
  (``objeto_corrupto``). La plataforma lee la versión vigente y verifica suma y tamaño, así que la
  ``version_id`` nueva no le importa.

Qué se pierde: los objetos de ``vigia-archive`` (los paquetes exportados se regeneran) y las
versiones anteriores de un objeto o de un secreto. También lo escrito en los
``VIGIA_SAVE_SECONDS`` anteriores a un reinicio o una caída; ``bootstrap.sh`` y ``admin.sh``
fuerzan un ``save`` al terminar, y los secretos solo cambian con una orden administrativa
(``rotate-key``) o un ``bootstrap``. La clave de la autoridad de nodos no pasa por aquí: vive en su
propio volumen (``VIGIA_NODE_CA_KEY_FILE``).

Corre dentro del contenedor de LocalStack (``/usr/local/bin/python3`` con ``boto3``). Ningún
valor de secreto sale por la salida estándar: solo nombres y recuentos. Del espejo, solo
recuentos: nunca una clave, un nombre de archivo ni un contenido.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, FlexibleChecksumError

ENDPOINT: Final = os.environ.get("VIGIA_LOCALSTACK_ENDPOINT", "http://localhost:4566")
STATE: Final = Path(os.environ.get("VIGIA_STATE_DIR", "/state"))
SNAPSHOT: Final = STATE / "localstack-snapshot.json"
MARKER: Final = Path(os.environ.get("VIGIA_RESTORE_MARKER", "/tmp/vigia-restored"))  # noqa: S108
MAX_OBJECT_BYTES: Final = 1024 * 1024
SENTINEL_KEY: Final = "health/ready-sentinel"
FORMAT_VERSION: Final = 1
# Espejo de vigia-evidence (TASK-608).
ALMACEN: Final = Path(os.environ.get("VIGIA_ALMACEN_DIR", "/almacen"))
EVIDENCE_MARKER: Final = Path(
    os.environ.get("VIGIA_EVIDENCE_RESTORE_MARKER", "/tmp/vigia-evidence-restored")  # noqa: S108
)
ALMACEN_MAX_BYTES: Final = 2 * 1024 * 1024 * 1024
INDEX_VERSION: Final = 1
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_MISSING: Final = frozenset({"404", "NoSuchKey", "NotFound"})
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
    # Antes de MARKER: el contenedor no da sano hasta que las evidencias han vuelto. Su marca propia
    # habilita las bajas del espejo (``mirror_evidence``).
    almacen = restore_evidence(s3, evidence)
    EVIDENCE_MARKER.write_text("restaurado\n", encoding="ascii")
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
        _setting("VIGIA_STORE_RELAY_SECRET_NAME"): {"binary": _read_state_file("store-relay.key")},
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
    print(f"restore almacen: {_counts(almacen)}")
    if almacen["objeto_corrupto"]:
        _event("objeto_corrupto", {"objetos": almacen["objeto_corrupto"]})
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
    _publish(path, text.encode("utf-8"))
    return True


def _publish(path: Path, data: bytes) -> None:
    """``data`` en ``path`` (``0600``): un temporal propio, ``fsync`` y ``os.replace``."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


# --- Espejo de vigia-evidence (TASK-608) --------------------------------------------------------


def _counts(counts: dict[str, int]) -> str:
    return " ".join(f"{name}={value}" for name, value in counts.items())


def _event(code: str, counts: dict[str, int]) -> None:
    """Una línea para el registro del contenedor: un código cerrado y recuentos, nada más.

    El bucle de ``ready.d`` tira la salida de ``save`` (cada 10 s) y pasa ``VIGIA_EVENT_LOG`` (el
    ``stderr`` del proceso 1, el registro del contenedor); si no se puede abrir, solo ``stderr``.
    """
    line = f"{code}: {_counts(counts)}"
    print(line, file=sys.stderr)
    target = os.environ.get("VIGIA_EVENT_LOG", "").strip()
    if target:
        with contextlib.suppress(OSError), Path(target).open("a", encoding="utf-8") as output:
            output.write(line + "\n")


def _blobs() -> Path:
    return ALMACEN / "objetos"


def _index_path() -> Path:
    return ALMACEN / "indice.json"


@contextlib.contextmanager
def _almacen_lock() -> Iterator[None]:
    """Un solo ``save`` o ``restore`` (o una copia de ``backup.sh``) a la vez sobre ``/almacen``."""
    ALMACEN.mkdir(parents=True, exist_ok=True)
    _blobs().mkdir(mode=0o700, exist_ok=True)
    _blobs().chmod(0o700)
    descriptor = os.open(ALMACEN / ".cerrojo", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def max_bytes() -> int:
    raw = os.environ.get("VIGIA_ALMACEN_MAX_BYTES", "").strip()
    if not raw:
        return ALMACEN_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit("VIGIA_ALMACEN_MAX_BYTES debe ser un número de bytes") from None
    if value < 0:
        raise SystemExit("VIGIA_ALMACEN_MAX_BYTES no puede ser negativo")
    return value


def load_index() -> dict[str, Any]:
    path = _index_path()
    if not path.exists():
        return {"version": INDEX_VERSION, "objetos": {}, "rechazados": {}, "omitidos_por_tope": 0}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != INDEX_VERSION:
        raise SystemExit("el índice del almacén tiene un formato que no conozco")
    return document  # type: ignore[no-any-return]


def full_object_sha256(response: dict[str, Any]) -> str | None:
    """La SHA-256 de objeto entero (hex) que declara el almacén, o ``None`` si no la tiene.

    Como ``ObjectHead.full_object_sha256_hex`` de la plataforma: una suma compuesta (subida por
    partes) no es la de los bytes.
    """
    if str(response.get("ChecksumType") or "FULL_OBJECT") != "FULL_OBJECT":
        return None
    try:
        digest = base64.b64decode(str(response.get("ChecksumSHA256") or ""), validate=True)
    except (binascii.Error, ValueError):
        return None
    return digest.hex() if len(digest) == 32 else None


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if _error_code(error) not in _MISSING:
            raise
        return False
    return True


def _read_blob(entry: dict[str, Any]) -> bytes | None:
    """Los bytes del blob de ``entry`` si su SHA-256 y su tamaño son los del índice."""
    digest = str(entry.get("sha256", ""))
    if not _SHA256_HEX.fullmatch(digest):
        return None
    try:
        data = (_blobs() / digest).read_bytes()
    except FileNotFoundError:
        return None
    if hashlib.sha256(data).hexdigest() != digest or len(data) != entry.get("size"):
        return None
    return data


def restore_evidence(s3: Any, bucket: str) -> dict[str, int]:
    """Sube cada clave del índice que falte en LocalStack; recuentos, nunca claves."""
    counts = {"restaurados": 0, "presentes": 0, "objeto_corrupto": 0}
    with _almacen_lock():
        for key, entry in sorted(load_index()["objetos"].items()):
            if _object_exists(s3, bucket, key):
                counts["presentes"] += 1
                continue
            data = _read_blob(entry)
            if data is None:
                counts["objeto_corrupto"] += 1
                continue
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=entry["content_type"],
                Metadata=dict(entry["metadata"]),
                ChecksumSHA256=base64.b64encode(bytes.fromhex(entry["sha256"])).decode("ascii"),
            )
            counts["restaurados"] += 1
    return counts


def _listing(s3: Any, bucket: str) -> dict[str, list[Any]]:
    """Clave → ``[ETag, tamaño]`` de las versiones vigentes, sin el centinela de salud."""
    found: dict[str, list[Any]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            if item["Key"] == SENTINEL_KEY:
                continue
            found[item["Key"]] = [str(item.get("ETag", "")), int(item["Size"])]
    return found


def _download(s3: Any, bucket: str, key: str) -> tuple[bytes | None, dict[str, Any]] | None:
    """La versión vigente y su respuesta (``None`` si se borró entre el listado y la descarga).

    ``botocore`` ya valida la suma al leer con ``ChecksumMode=ENABLED``: si falla, los bytes son
    ``None`` (suma distinta) y no se guardan.
    """
    try:
        response = s3.get_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as error:
        if _error_code(error) not in _MISSING:
            raise
        return None
    try:
        body = response["Body"].read()
    except FlexibleChecksumError:
        return None, response
    return body, response


def _store_blob(digest: str, data: bytes) -> int:
    """Guarda ``data`` como ``objetos/<digest>``; devuelve los bytes que ocupa de más."""
    target = _blobs() / digest
    with contextlib.suppress(FileNotFoundError):
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return 0
    _publish(target, data)
    return len(data)


def _stored_bytes() -> int:
    return sum(
        path.stat().st_size for path in _blobs().iterdir() if _SHA256_HEX.fullmatch(path.name)
    )


def mirror_evidence(s3: Any, bucket: str, *, restored: bool, limit: int) -> dict[str, int]:
    """Copia incremental de ``bucket`` en ``/almacen``; recuentos, nunca claves.

    Solo descarga las claves nuevas o con otro ``ETag`` o tamaño. El índice solo nombra blobs ya
    publicados y verificados, así que, suba lo que suba la plataforma a la vez, o no tiene la clave
    o la tiene con un blob cuya suma coincide. Las bajas, solo con ``restored``.
    """
    counts = dict.fromkeys(
        ("nuevos", "sin_cambios", "bajas", "suma_distinta", "sin_suma", "omitidos_por_tope"), 0
    )
    with _almacen_lock():
        document = load_index()
        objects: dict[str, dict[str, Any]] = dict(document["objetos"])
        previously_rejected: dict[str, list[Any]] = document.get("rechazados", {})
        rejected: dict[str, list[Any]] = {}
        listed = _listing(s3, bucket)
        used = _stored_bytes()
        for key, seen in sorted(listed.items()):
            entry = objects.get(key)
            if entry is not None and [entry["etag"], entry["size"]] == seen:
                counts["sin_cambios"] += 1
                continue
            if previously_rejected.get(key) == seen:
                rejected[key] = seen
                continue
            if used + seen[1] > limit:
                counts["omitidos_por_tope"] += 1
                continue
            found = _download(s3, bucket, key)
            if found is None:
                continue
            data, response = found
            expected = full_object_sha256(response)
            if expected is None:
                counts["sin_suma"] += 1
                rejected[key] = seen
                continue
            digest = "" if data is None else hashlib.sha256(data).hexdigest()
            if data is None or digest != expected or len(data) != response.get("ContentLength"):
                counts["suma_distinta"] += 1
                rejected[key] = seen
                continue
            used += _store_blob(digest, data)
            objects[key] = {
                "sha256": digest,
                "size": len(data),
                "content_type": str(response.get("ContentType") or "application/octet-stream"),
                "metadata": {str(k): str(v) for k, v in (response.get("Metadata") or {}).items()},
                "etag": str(response.get("ETag", "")),
            }
            counts["nuevos"] += 1
        if restored:
            for key in sorted(set(objects) - set(listed)):
                del objects[key]
                counts["bajas"] += 1
        full = counts["omitidos_por_tope"]
        index = {
            "version": INDEX_VERSION,
            "objetos": objects,
            "rechazados": rejected,
            "omitidos_por_tope": full,
        }
        # Primero el índice y después los huérfanos (los que ninguna clave nombra, también una
        # versión sustituida o un temporal de un ``save`` que murió): nunca un índice que nombra un
        # blob borrado. Sin ``restored`` ninguna clave sale del índice, y sus blobs siguen.
        write_atomically(_index_path(), json.dumps(index, sort_keys=True))
        referenced = {entry["sha256"] for entry in objects.values()}
        for path in _blobs().iterdir():
            if path.name not in referenced:
                path.unlink()
    if full and full != document.get("omitidos_por_tope", 0):
        _event(
            "almacen_lleno",
            {"omitidos": full, "guardados": len(objects), "bytes": used, "tope": limit},
        )
    if counts["suma_distinta"]:
        _event("objeto_no_guardado", {"suma_distinta": counts["suma_distinta"]})
    return counts


def save() -> int:
    if not MARKER.exists():
        print("save: sin restore terminado; no se escribe nada", file=sys.stderr)
        return 3
    s3 = _client("s3")
    snapshot = collect(_client("secretsmanager"), s3, _setting("VIGIA_EDGE_BUCKET"))
    changed = write_atomically(SNAPSHOT, json.dumps(snapshot, sort_keys=True))
    print(
        f"save: {len(snapshot['secrets'])} secretos y {len(snapshot['objects'])} objetos"
        f" ({'escrita' if changed else 'sin cambios'})"
    )
    almacen = mirror_evidence(
        s3,
        _setting("VIGIA_EVIDENCE_BUCKET"),
        restored=EVIDENCE_MARKER.exists(),
        limit=max_bytes(),
    )
    print(f"save almacen: {_counts(almacen)}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"restore", "save"}:
        print("uso: state.py restore|save", file=sys.stderr)
        return 2
    return restore() if argv[1] == "restore" else save()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
