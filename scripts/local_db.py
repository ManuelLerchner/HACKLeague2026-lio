"""Start a workspace-local PostgreSQL cluster when Docker is unavailable."""

import os
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

root = Path(".data/postgres").resolve()
root.parent.mkdir(exist_ok=True)
if not (root / "PG_VERSION").exists():
    with NamedTemporaryFile(mode="w", dir=root.parent) as password:
        password.write("chaser\n")
        password.flush()
        subprocess.run(
            [
                "initdb",
                "-D",
                str(root),
                "-U",
                "chaser",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                f"--pwfile={password.name}",
            ],
            check=True,
        )
if subprocess.run(["pg_ctl", "-D", str(root), "status"], capture_output=True).returncode:
    subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(root),
            "-l",
            str(root.parent / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p 5432 -k {root.parent}",
            "start",
        ],
        check=True,
    )
subprocess.run(
    [
        "psql",
        "-h",
        str(root.parent),
        "-U",
        "chaser",
        "-d",
        "postgres",
        "-c",
        "ALTER ROLE chaser WITH PASSWORD 'chaser'",
    ],
    check=True,
    stdout=subprocess.DEVNULL,
)
exists = subprocess.run(
    [
        "psql",
        "-h",
        str(root.parent),
        "-U",
        "chaser",
        "-d",
        "postgres",
        "-tAc",
        "SELECT 1 FROM pg_database WHERE datname = 'chaser'",
    ],
    capture_output=True,
    text=True,
    check=True,
)
if not exists.stdout.strip():
    subprocess.run(["createdb", "-h", str(root.parent), "-U", "chaser", "chaser"], check=True)
print(f"PostgreSQL ready on 127.0.0.1:5432 (PID {os.getpid()} setup completed)")
