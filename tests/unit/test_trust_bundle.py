"""The CA bundle BoxMedia pins, and that the image really verifies against it.

httpx verifies against `SSL_CERT_FILE` when it is set and certifi's bundle otherwise, and the
distroless base sets that variable to its own Debian bundle. Both stores trusted `emSign ECC Root
CA - G3`, the path to eight eMudhra intermediate CAs whose private keys can be recovered from
their own certificates (PYSEC-2026-4320 / GHSA-6q48-6c7w-9gw2), and the 1.5.1 image completed a
handshake with a certificate one of them had issued.

Updating certifi alone turned pip-audit and Trivy green while that handshake still succeeded,
because they judge the pin and the image used the other store. These tests hold all three links:
the pinned bundle is clean, the image uses it, and the bundle tested is the bundle shipped.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parents[2]

# Every root the eight intermediates chain to, by SHA-256, as published with the incident:
# https://emudhra.we-have-a-problem.com/roots.json
COMPROMISED_CHAIN_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
    "7dd78d5f4f13459a83dff9abbba62edbaf6f2d102bf257fd712f4d9f2746ed8d": "emSign Root TLS CA - G3",
    "f2edaaff3d9b701fb4d14c9f285f8c0bad90fc3bcc1fa07d47f4718edaba34a5": "emSign Root CS CA - G3",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": "AAA Certificate Services",
}
CERTIFI_PIN = re.compile(r"^certifi==(\S+)", re.MULTILINE)
RUNTIME_STAGE_MARKER = " AS runtime"
BUNDLE_IN_THE_IMAGE = "/app/deps/certifi/cacert.pem"


def test_the_pinned_bundle_trusts_no_root_behind_the_compromised_intermediates() -> None:
    context = ssl.create_default_context(cafile=certifi.where())
    trusted = {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True)}

    assert trusted, "an empty trust store would pass this by refusing every certificate"
    still_trusted = {name for root, name in COMPROMISED_CHAIN_ROOTS.items() if root in trusted}
    assert still_trusted == set()


def test_the_image_verifies_against_the_pinned_bundle() -> None:
    """Without this the pin above decides nothing in production: the base's SSL_CERT_FILE wins.

    A wrong path fails closed, refusing every certificate, so the last assert guards against a
    certifi release moving its bundle out from under the Dockerfile.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime_stage = dockerfile.split(RUNTIME_STAGE_MARKER, 1)[1]

    assert f"SSL_CERT_FILE={BUNDLE_IN_THE_IMAGE}" in runtime_stage
    assert "COPY --from=builder /install /app/deps" in runtime_stage
    assert certifi.where().endswith("/certifi/cacert.pem")


def test_the_image_and_the_test_environment_pin_one_bundle() -> None:
    """The first test reads the installed environment, built from requirements.lock, while the
    image installs requirements-runtime.txt. If they differed it would vouch for a bundle that
    never ships."""
    shipped = CERTIFI_PIN.findall((ROOT / "requirements-runtime.txt").read_text(encoding="utf-8"))
    tested = CERTIFI_PIN.findall((ROOT / "requirements.lock").read_text(encoding="utf-8"))

    assert shipped, "requirements-runtime.txt no longer pins certifi"
    assert shipped == tested
