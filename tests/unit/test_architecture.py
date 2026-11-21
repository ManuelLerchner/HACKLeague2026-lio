import ast
from pathlib import Path


def test_domain_and_ports_do_not_import_infrastructure():
    for folder in ("domain", "ports"):
        for path in (Path("src/supplier_chaser") / folder).glob("*.py"):
            tree = ast.parse(path.read_text())
            modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            modules += [
                alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
            ]
            assert not any(
                part in module
                for module in modules
                for part in ("supplier_chaser.adapters", "supplier_chaser.web", "sqlalchemy")
            ), path
