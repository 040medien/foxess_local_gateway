"""Certificate generation for the local FoxESS cloud emulator."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


FOXESS_CERT_IPS = [
    "139.224.232.119",
    "8.209.116.72",
    "8.209.80.124",
    "47.91.86.144",
    "139.196.46.234",
    "8.211.20.96",
    "47.254.142.98",
    "8.209.79.219",
    "47.254.159.190",
    "101.133.233.208",
    "47.102.203.29",
]
FOXESS_CERT_DNS = ["*.maitian-yun.com", "*.foxesscloud.com", "foxesscloud.com", "www.foxesscloud.com"]


def ensure_cert(cert: Path, key: Path, force: bool = False) -> None:
    if cert.exists() and key.exists() and not force:
        return
    cert.parent.mkdir(parents=True, exist_ok=True)
    key.parent.mkdir(parents=True, exist_ok=True)
    san_values = [f"IP:{ip}" for ip in FOXESS_CERT_IPS]
    san_values.extend(f"DNS:{name}" for name in FOXESS_CERT_DNS)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "36500",
            "-keyout",
            os.fspath(key),
            "-out",
            os.fspath(cert),
            "-subj",
            "/C=CN/ST=JiangSu/L=Wuxi/O=FoxESS/CN=monitor",
            "-addext",
            f"subjectAltName={','.join(san_values)}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
