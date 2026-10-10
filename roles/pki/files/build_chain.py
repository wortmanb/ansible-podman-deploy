#!/usr/bin/env python3
"""Assemble a certificate chain in issuer order for serving.

Usage: build_chain.py CERT_FILE CA_BUNDLE OUT_FILE

Starts from the first certificate in CERT_FILE (the leaf) and repeatedly
appends its issuer, found among the other certificates in CERT_FILE and
CA_BUNDLE, skipping duplicates, until it reaches a self-signed root or no
issuer is available. Java (Elasticsearch) rejects chains that are out of
order or contain duplicates.

Writes OUT_FILE only if its content changes, and prints JSON:
{"changed": bool, "length": int, "top_fingerprint": "<SHA-256 hex>"}
"""
import json
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


def load(path):
    return x509.load_pem_x509_certificates(Path(path).read_bytes())


def main(cert_file, ca_bundle, out_file):
    certs = load(cert_file)
    pool = certs[1:] + load(ca_bundle)
    chain = [certs[0]]
    seen = {certs[0].fingerprint(hashes.SHA256())}
    while chain[-1].issuer != chain[-1].subject:
        issuer = next(
            (c for c in pool
             if c.subject == chain[-1].issuer
             and c.fingerprint(hashes.SHA256()) not in seen),
            None)
        if issuer is None:
            break
        chain.append(issuer)
        seen.add(issuer.fingerprint(hashes.SHA256()))

    pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain)
    out = Path(out_file)
    changed = not out.exists() or out.read_bytes() != pem
    if changed:
        out.write_bytes(pem)
        out.chmod(0o644)
    print(json.dumps({
        "changed": changed,
        "length": len(chain),
        "top_fingerprint": chain[-1].fingerprint(hashes.SHA256()).hex().upper(),
    }))


if __name__ == "__main__":
    main(*sys.argv[1:4])
