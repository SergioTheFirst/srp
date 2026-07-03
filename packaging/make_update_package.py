"""Упаковка dist/share в архив автообновления для server/updates/.

build.bat вызывает этот скрипт последним шагом сборки: пакует уже собранный
``dist/share`` (setup.exe + payload/**) в
``dist/updates/srp-agent-update-<ver>.zip`` и кладёт рядом ``manifest.json`` --
ровно то, что сервер (server/updates.py) потом отдаёт агентам на
самообновление. Оператор вручную копирует оба файла в ``server/updates/`` на
сервере; сам скрипт никуда не грузит и с сетью не работает (см.
docs/superpowers/plans/2026-07-03-agent-auto-update-plan.md).

Build-only: не часть рантайма ни агента, ни сервера -- чистый stdlib (zipfile,
hashlib, json, pathlib), запускается на dev-машине один раз на релиз.

Usage: python packaging/make_update_package.py [share_dir] [out_dir]
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Optional

# Потоковое чтение при подсчёте sha256 -- архив может быть десятки МБ, не
# держим его целиком в памяти.
_HASH_CHUNK_BYTES = 1024 * 1024

# НЕ упаковываем: config.template.json -- политика организации, ей место
# только на ACL'd-шаре, а не в архиве, который по сети качает весь парк
# агентов. Любой *.bat тоже не пакуем -- в готовых BAT подставлены секреты
# (--token/--password, см. docs/agent-install.md §3); эта проверка идёт
# отдельно, по расширению файла.
_EXCLUDED_PAYLOAD_NAMES = {"config.template.json"}


def _parse_version(value: str) -> tuple[int, int, int]:
    """Строго разобрать версию "MAJOR.MINOR.PATCH" в тройку чисел.

    Маленький локальный парсер -- не импортирует shared.schema.parse_version,
    чтобы build-скрипт не тянул зависимость на pydantic-контракт ради трёх чисел.
    """
    parts = value.split(".")
    if len(parts) != 3:
        raise ValueError(f"версия должна быть в формате MAJOR.MINOR.PATCH, получено: {value!r}")
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(
            f"версия должна состоять из целых чисел MAJOR.MINOR.PATCH, получено: {value!r}"
        ) from exc
    return (major, minor, patch)


def build_package(share_dir: Path, out_dir: Path, version: str) -> dict[str, Any]:
    """Собрать srp-agent-update-<version>.zip + manifest.json в out_dir.

    Чистая функция (без CLI/аргументов) -- юнит-тестируется на временных
    каталогах, без реальной сборки PyInstaller. Возвращает тот же
    манифест-словарь, что записывается в manifest.json.
    """
    _parse_version(version)

    setup_exe = share_dir / "setup.exe"
    if not setup_exe.is_file():
        raise ValueError(f"не найден установщик {setup_exe} -- сначала соберите build.bat")

    payload_dir = share_dir / "payload"
    if not payload_dir.is_dir():
        raise ValueError(f"не найден каталог {payload_dir} -- сначала соберите build.bat")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"srp-agent-update-{version}.zip"
    zip_path = out_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(setup_exe, arcname="setup.exe")
        version_file = share_dir / "VERSION"
        if version_file.is_file():
            archive.write(version_file, arcname="VERSION")
        for path in sorted(payload_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".bat" or path.name in _EXCLUDED_PAYLOAD_NAMES:
                continue
            rel = path.relative_to(payload_dir).as_posix()
            archive.write(path, arcname=f"payload/{rel}")

    digest = hashlib.sha256()
    with zip_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)

    manifest: dict[str, Any] = {
        "version": version,
        "file": zip_name,
        "sha256": digest.hexdigest(),
        "size": zip_path.stat().st_size,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: python packaging/make_update_package.py [share_dir] [out_dir]."""
    args = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parents[1]
    share_dir = Path(args[0]) if len(args) > 0 else root / "dist" / "share"
    out_dir = Path(args[1]) if len(args) > 1 else root / "dist" / "updates"
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    try:
        manifest = build_package(share_dir, out_dir, version)
    except ValueError as exc:
        print(f"ошибка сборки пакета обновления: {exc}", file=sys.stderr)
        return 1

    print(f"пакет обновления готов: {out_dir / manifest['file']}")
    print("скопируйте оба файла в server/updates/ на сервере SRP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
