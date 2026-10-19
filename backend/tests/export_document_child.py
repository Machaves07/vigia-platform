"""Hijo de ``test_export_document_determinism.py``: genera el documento y escribe su
``sha256`` en ``stdout``. Se lanza con ``python -m tests.export_document_child`` desde un
proceso con su propia zona horaria y su propia semilla de hash, para comprobar que
``document_sha256`` no depende de ninguna de las dos (VIG-236)."""

from __future__ import annotations

import hashlib
import sys

from tests.export_document_support import sample_manifest_inputs
from vigia_platform.accreditation.document import generate_document


def main() -> None:
    pdf = generate_document(sample_manifest_inputs())
    sys.stdout.write(hashlib.sha256(pdf).hexdigest())


if __name__ == "__main__":
    main()
