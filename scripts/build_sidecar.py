"""Tauri v2 masaüstü kurulumu için bağımsız `seslinot-server` PyInstaller sidecar üreticisi."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "binaries"
TAURI_CONF = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json"


def main() -> int:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    bin_name = "seslinot-server.exe" if os.name == "nt" else "seslinot-server"
    dist_dir = REPO_ROOT / "build" / "sidecar-dist"
    work_dir = REPO_ROOT / "build" / "sidecar-work"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "seslinot-server",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--collect-submodules",
        "seslinot",
        "--collect-submodules",
        "uvicorn",
        "--collect-submodules",
        "starlette",
        "--collect-data",
        "trafilatura",
        "--collect-data",
        "dateparser",
        "--collect-data",
        "htmldate",
        "--hidden-import",
        "seslinot.audio._sd_record",
    ]
    for opt_pkg in ("sounddevice", "_sounddevice_data"):
        try:
            __import__(opt_pkg)
            cmd.extend(["--collect-all", opt_pkg])
        except ImportError:
            pass
    cmd.append(str(REPO_ROOT / "scripts" / "sidecar_entry.py"))
    print("Running:", " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False).returncode
    if rc != 0:
        return rc

    built = dist_dir / bin_name
    if not built.is_file():
        print(f"Beklenen sidecar bulunamadı: {built}", file=sys.stderr)
        return 1

    target = BIN_DIR / bin_name
    shutil.copy2(built, target)
    if os.name != "nt":
        os.chmod(target, 0o755)
    print(f"Sidecar hazır: {target} ({target.stat().st_size / (1024 * 1024):.1f} MB)")

    # Release derlemesinde `resources: ["binaries/*"]` paketlensin
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    bundle = conf.setdefault("bundle", {})
    resources = list(bundle.get("resources") or [])
    if "binaries/*" not in resources:
        resources.append("binaries/*")
        bundle["resources"] = resources
        TAURI_CONF.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
