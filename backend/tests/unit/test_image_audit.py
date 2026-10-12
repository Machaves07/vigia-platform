"""``tools/image_audit.py`` sobre imágenes sintéticas con el formato de ``docker save`` (TASK-143).

Criterio de aceptación: ``docker history`` y el contenido de las capas no contienen ninguna clave
privada, y la imagen corre con un usuario no root. Cada prueba arma una imagen mínima (manifiesto,
configuración y capas en tar) con la clave en un sitio distinto: en un archivo, en una capa que
la siguiente borra, en una orden del historial, partida entre dos trozos de lectura, con nombre de
clave de SSH. La clave se genera en cada ejecución (Ed25519 en formato OpenSSH): ningún secreto
en el árbol.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import image_audit
from tools.image_audit import AuditError, audit_image, main, needles_from_key


def _openssh_key() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )


def _pkcs8_key() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )


def _layer(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as layer:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            layer.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _image(
    tmp_path: Path,
    layers: list[dict[str, bytes]],
    *,
    user: str | None = "10001:10001",
    history: list[str] | None = None,
) -> Path:
    config: dict[str, Any] = {
        "config": {} if user is None else {"User": user},
        "history": [{"created_by": text} for text in (history or ["RUN uv sync --frozen"])],
    }
    names = [f"blobs/sha256/layer{index}" for index in range(len(layers))]
    path = tmp_path / "image.tar"
    with tarfile.open(path, mode="w") as archive:
        entries = {
            "manifest.json": json.dumps(
                [{"Config": "blobs/sha256/config", "RepoTags": [], "Layers": names}]
            ).encode(),
            "blobs/sha256/config": json.dumps(config).encode(),
        }
        entries.update({name: _layer(files) for name, files in zip(names, layers, strict=True)})
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


_CLEAN = {"app/src/vigia_platform/__init__.py": b'"""Vigia."""\n', "etc/passwd": b"vigia:x:10001"}


def test_a_clean_non_root_image_passes(tmp_path: Path) -> None:
    key = _openssh_key()
    image = _image(tmp_path, [_CLEAN, {"app/.venv/bin/python": b"#!"}])
    assert audit_image(image, needles_from_key(key)) == []


def test_needles_skip_the_pem_armor_and_short_lines() -> None:
    key = _openssh_key()
    needles = needles_from_key(key)
    assert needles
    assert all(b"-----" not in needle and len(needle) >= 16 for needle in needles)
    assert needles_from_key("-----BEGIN X-----\nabc\n-----END X-----\n") == []


@pytest.mark.parametrize(
    "user", [None, "", "root", "0", "0:0", "root:vigia", "00", "000:10001", " 0"]
)
def test_a_root_image_fails(tmp_path: Path, user: str | None) -> None:
    findings = audit_image(_image(tmp_path, [_CLEAN], user=user))
    assert [f.where for f in findings] == ["config.User"]


def test_the_key_in_a_layer_file_fails_without_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = _openssh_key()
    image = _image(tmp_path, [_CLEAN, {"root/.cache/deploy": key.encode()}])
    key_file = tmp_path / "deploy_key"
    key_file.write_text(key)
    assert main([str(image), "--needle-file", str(key_file)]) == 1
    output = capsys.readouterr()
    assert "capa 2:/root/.cache/deploy" in output.err
    for needle in needles_from_key(key):
        assert needle.decode() not in output.out + output.err


def test_the_key_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _openssh_key()
    monkeypatch.setenv("DEPLOY_KEY", key)
    leaked = _image(tmp_path, [{"opt/k": needles_from_key(key)[0]}])
    assert main([str(leaked), "--needle-env", "DEPLOY_KEY"]) == 1
    monkeypatch.setenv("DEPLOY_KEY", "")
    assert main([str(leaked), "--needle-env", "DEPLOY_KEY"]) == 2  # vacía: no se audita a ciegas
    monkeypatch.delenv("DEPLOY_KEY")
    assert main([str(leaked), "--needle-env", "DEPLOY_KEY"]) == 2


def test_a_key_deleted_by_a_later_layer_still_fails(tmp_path: Path) -> None:
    key = _openssh_key()
    image = _image(
        tmp_path,
        [{"tmp/k": key.encode()}, {"tmp/.wh.k": b""}],
    )
    findings = audit_image(image, needles_from_key(key))
    assert [f.where for f in findings] == ["capa 1:/tmp/k"]


def test_one_line_of_the_key_is_enough(tmp_path: Path) -> None:
    key = _openssh_key()
    line = needles_from_key(key)[-1]
    image = _image(tmp_path, [{"app/config.txt": b"value=" + line + b"\n"}])
    assert len(audit_image(image, needles_from_key(key))) == 1


def test_a_key_split_across_two_read_chunks_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_audit, "_CHUNK", 64)
    key = _openssh_key()
    padding = b"x" * 50
    image = _image(tmp_path, [{"app/blob": padding + key.encode() + padding}])
    assert len(audit_image(image, needles_from_key(key))) == 1
    # Sin el material: el bloque PEM completo, partido entre trozos, también se detecta.
    assert len(audit_image(image)) == 1


def test_any_complete_private_key_block_fails_even_without_needles(tmp_path: Path) -> None:
    image = _image(tmp_path, [{"opt/a.pem": _pkcs8_key().encode()}])
    assert [f.problem for f in audit_image(image)] == ["contiene un bloque PEM de clave privada"]


def test_the_bare_pem_header_of_a_crypto_library_is_not_a_key(tmp_path: Path) -> None:
    constant = b'_SK_START = b"-----BEGIN OPENSSH PRIVATE KEY-----"\n_SK_END = b"-----END"\n'
    image = _image(tmp_path, [{"app/.venv/lib/ssh.py": constant}])
    assert audit_image(image) == []


def test_the_key_in_the_history_fails(tmp_path: Path) -> None:
    key = _openssh_key()
    line = needles_from_key(key)[0].decode()
    image = _image(tmp_path, [_CLEAN], history=[f"RUN echo {line} > /k"])
    findings = audit_image(image, needles_from_key(key))
    assert [f.where for f in findings] == ["history[0]"]


@pytest.mark.parametrize(
    "name",
    ["root/.ssh/id_ed25519", "home/x/.ssh/id_rsa", "id_ecdsa_sk", "root/.ssh/config_key"],
)
def test_ssh_key_names_and_ssh_directories_fail(tmp_path: Path, name: str) -> None:
    findings = audit_image(_image(tmp_path, [{name: b"no es una clave"}]))
    assert len(findings) == 1


def test_known_hosts_are_allowed(tmp_path: Path) -> None:
    files = {
        "etc/ssh/ssh_known_hosts": b"github.com ssh-ed25519 AAAA",
        "root/.ssh/known_hosts": b"",
    }
    assert audit_image(_image(tmp_path, [files])) == []


def test_not_an_image_is_an_error(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.tar"
    with tarfile.open(bogus, mode="w") as archive:
        info = tarfile.TarInfo("readme")
        archive.addfile(info, io.BytesIO(b""))
    with pytest.raises(AuditError):
        audit_image(bogus)
    assert main([str(bogus)]) == 2
    assert main([str(tmp_path / "missing.tar")]) == 2
    empty_key = tmp_path / "empty"
    empty_key.write_text("-----BEGIN-----\n")
    assert main([str(_image(tmp_path, [_CLEAN])), "--needle-file", str(empty_key)]) == 2


# --- Fuentes del documento del acta (TASK-217, NFR-GOB-32) ---------------------------------------

_FONTS = {"noto-sans/NotoSans-Regular.ttf": b"\x00\x01\x00\x00regular", "noto-sans/OFL.txt": b"OFL"}


def _fonts_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "fonts"
    for name, data in _FONTS.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return directory


def _with_fonts(**changes: bytes) -> dict[str, bytes]:
    files = {f"app/resources/fonts/{name}": data for name, data in _FONTS.items()}
    files.update(changes)
    return files


def test_the_packaged_fonts_of_the_repository_are_required() -> None:
    required = image_audit.packaged_fonts()
    assert {path.name for path in required} == {
        "NotoSans-Regular.ttf",
        "NotoSans-Bold.ttf",
        "OFL.txt",
    }
    assert all(str(path).startswith("app/resources/fonts/noto-sans/") for path in required)


def test_an_image_with_its_packaged_fonts_passes(tmp_path: Path) -> None:
    fonts = image_audit.packaged_fonts(_fonts_dir(tmp_path))
    image = _image(tmp_path, [_CLEAN, _with_fonts()])
    assert audit_image(image, fonts=fonts) == []
    assert main([str(image), "--fonts-dir", str(tmp_path / "fonts")]) == 0


def test_a_missing_or_different_font_fails(tmp_path: Path) -> None:
    fonts = image_audit.packaged_fonts(_fonts_dir(tmp_path))
    missing = _with_fonts()
    del missing["app/resources/fonts/noto-sans/NotoSans-Regular.ttf"]
    findings = audit_image(_image(tmp_path, [_CLEAN, missing]), fonts=fonts)
    assert [f.problem for f in findings] == ["falta la fuente empaquetada del documento del acta"]
    changed = _with_fonts(**{"app/resources/fonts/noto-sans/OFL.txt": b"otra licencia"})
    findings = audit_image(_image(tmp_path, [_CLEAN, changed]), fonts=fonts)
    assert [f.problem for f in findings] == ["la fuente de la imagen no es la del repositorio"]
    # Sin la carpeta del repositorio no se audita a ciegas.
    assert main([str(_image(tmp_path, [_CLEAN])), "--fonts-dir", str(tmp_path / "nada")]) == 2


@pytest.mark.parametrize(
    "system_font",
    ["usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "usr/local/share/fonts/x.otf"],
)
def test_a_system_font_in_any_layer_fails(tmp_path: Path, system_font: str) -> None:
    fonts = image_audit.packaged_fonts(_fonts_dir(tmp_path))
    image = _image(tmp_path, [_CLEAN, {system_font: b"fuente"}, _with_fonts()])
    findings = audit_image(image, fonts=fonts)
    assert [f.problem for f in findings] == ["fuente del sistema: solo valen las empaquetadas"]
    assert main([str(image), "--fonts-dir", str(tmp_path / "fonts")]) == 1


def test_by_default_the_command_requires_the_repository_fonts(tmp_path: Path) -> None:
    assert main([str(_image(tmp_path, [_CLEAN]))]) == 1
