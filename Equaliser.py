#!/usr/bin/env python3
"""
Equaliser.py - Per-mod weapon equaliser driven by your openmw.cfg load order.

Reads the user's openmw.cfg, walks every ``content=`` plugin in load order,
resolves each one against the configured ``data=`` directories, and for every
plugin that contains weapons emits a ``<plugin>_equalised.omwaddon`` override
that sets each weapon's chop / slash / thrust min and max to the
highest-average attack of that weapon (vanilla "always use best attack"
semantics).

Vanilla masters (Morrowind / Tribunal / Bloodmoon) are skipped by default
since the prebuilt BestAttackEqualized addon already covers them - edit
``SKIP_PLUGINS`` below to change that.

Generated patches are written into an ``equaliser_patched`` subfolder next
to Equaliser.py. Intermediate JSON files produced during the run are cleaned
up at the end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False


TES3CONV_URL = "https://github.com/Greatness7/tes3conv/releases/latest"

PLUGIN_EXTS = (".esm", ".esp", ".omwaddon")
EQUALISED_SUFFIX = "_equalised"
DUMP_SUFFIX = ".dump.json"
OUTPUT_DIR_NAME = "equaliser_patched"
# Plugins that are already covered by the prebuilt BestAttackEqualized addon.
# Compared case-insensitively against the plugin file name.
SKIP_PLUGINS = {
    "morrowind.esm",
    "tribunal.esm",
    "bloodmoon.esm",
}


class Tes3convNotFound(Exception):
    """Raised when tes3conv.exe cannot be located."""


def default_openmw_cfg() -> Path | None:
    # Windows default: %USERPROFILE%\Documents\My Games\OpenMW\openmw.cfg
    candidate = Path.home() / "Documents" / "My Games" / "OpenMW" / "openmw.cfg"
    return candidate if candidate.is_file() else None


def prompt_openmw_cfg() -> Path:
    found = default_openmw_cfg()
    if found is not None:
        return found
    if HAS_TK:
        root = tk.Tk()
        root.withdraw()
        preferred_dir = Path.home() / "Documents" / "My Games" / "OpenMW"
        initial_dir = preferred_dir if preferred_dir.is_dir() else Path.home()
        chosen = filedialog.askopenfilename(
            title="Select your openmw.cfg",
            initialdir=str(initial_dir),
            filetypes=[("OpenMW config", "openmw.cfg"), ("All files", "*.*")],
        )
        root.destroy()
        if chosen:
            return Path(chosen)
    raw = input("Path to openmw.cfg: ").strip().strip('"')
    return Path(raw)


def parse_openmw_cfg(cfg_path: Path) -> tuple[list[Path], list[str]]:
    data_dirs: list[Path] = []
    content: list[str] = []
    with cfg_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if key in ("data", "data-local"):
                data_dirs.append(Path(value))
            elif key == "content":
                content.append(value)
    return data_dirs, content


def find_tes3conv(script_dir: Path) -> Path | None:
    for candidate in (
        script_dir / "tes3conv.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def prompt_tes3conv(script_dir: Path) -> Path:
    found = find_tes3conv(script_dir)
    if found is not None:
        return found
    if HAS_TK:
        root = tk.Tk()
        root.withdraw()
        preferred_dir = script_dir 
        initial_dir = preferred_dir if preferred_dir.is_dir() else script_dir
        chosen = filedialog.askopenfilename(
            title="Locate tes3conv.exe",
            initialdir=str(initial_dir),
            filetypes=[
                ("tes3conv.exe", "tes3conv.exe"),
                ("Executable", "*.exe"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        if chosen:
            picked = Path(chosen)
            if picked.is_file():
                return picked
    raise Tes3convNotFound(
        "tes3conv.exe not found next to Equaliser.py or in chosen folder. "
        f"Files'.\n\nDownload it from:\n{TES3CONV_URL}"
    )


def show_tes3conv_missing_dialog(message: str) -> None:
    if HAS_TK:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("tes3conv.exe not found", message)
        root.destroy()
    else:
        print(message, file=sys.stderr)


def resolve_plugin(
    name: str,
    data_dirs: list[Path],
    dir_cache: dict[Path, dict[str, Path]],
) -> Path | None:
    # OpenMW resolves later data= entries first, so honour reverse order.
    for d in reversed(data_dirs):
        candidate = d / name
        if candidate.is_file():
            return candidate
    lower = name.lower()
    for d in reversed(data_dirs):
        if d not in dir_cache:
            try:
                dir_cache[d] = {p.name.lower(): p for p in d.iterdir() if p.is_file()}
            except OSError:
                dir_cache[d] = {}
        hit = dir_cache[d].get(lower)
        if hit is not None:
            return hit
    return None


def run_tes3conv(tes3conv: Path, src: Path, dst: Path) -> None:
    subprocess.run(
        [str(tes3conv), str(src), str(dst)],
        check=True,
        capture_output=True,
        text=True,
    )


def equalise_weapon(rec: dict) -> dict | None:
    new_rec = deepcopy(rec)
    d = new_rec.get("data") or {}
    chop = (d.get("chop_min", 0), d.get("chop_max", 0))
    slash = (d.get("slash_min", 0), d.get("slash_max", 0))
    thrust = (d.get("thrust_min", 0), d.get("thrust_max", 0))
    best_min, best_max = max(
        [chop, slash, thrust], key=lambda r: (r[0] + r[1]) / 2
    )
    if best_max <= 0:
        return None
    for fld, val in (
        ("chop_min", best_min), ("chop_max", best_max),
        ("slash_min", best_min), ("slash_max", best_max),
        ("thrust_min", best_min), ("thrust_max", best_max),
    ):
        if fld in d:
            d[fld] = val
    return new_rec


def build_patch(records: list[dict], plugin: Path) -> list[dict]:
    header = next((r for r in records if r.get("type") == "Header"), None)
    if header is None:
        raise ValueError(f"no Header record in {plugin.name}")

    out_weapons = []
    for rec in records:
        if rec.get("type") != "Weapon":
            continue
        eq = equalise_weapon(rec)
        if eq is not None:
            out_weapons.append(eq)
    if not out_weapons:
        return []

    # Patch loads after the mod it patches and lists that mod (plus its own
    # masters) as masters, so the override resolves cleanly in OpenMW.
    masters = [list(m) for m in header.get("masters", [])]
    if plugin.suffix.lower() != ".omwaddon":
        masters.append([plugin.name, plugin.stat().st_size])

    new_header = deepcopy(header)
    new_header["author"] = "Equaliser"
    new_header["description"] = f"Weapon equalisation patch for {plugin.name}"
    new_header["file_type"] = "Esp"
    new_header["num_objects"] = len(out_weapons)
    new_header["masters"] = masters

    return [new_header] + out_weapons


def cleanup_jsons(paths: list[Path]) -> None:
    for j in paths:
        if not j.exists():
            continue
        try:
            j.unlink()
        except OSError as e:
            print(f"  ! could not remove {j.name}: {e}")


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    output_dir = script_dir / OUTPUT_DIR_NAME
    output_dir.mkdir(exist_ok=True)

    cfg_path = prompt_openmw_cfg()
    if not cfg_path or not cfg_path.is_file():
        print(f"openmw.cfg not found: {cfg_path}", file=sys.stderr)
        return 1

    try:
        tes3conv = prompt_tes3conv(script_dir)
    except Tes3convNotFound as e:
        show_tes3conv_missing_dialog(str(e))
        return 1

    data_dirs, content = parse_openmw_cfg(cfg_path)
    if not data_dirs:
        print(f"No data= entries found in {cfg_path}", file=sys.stderr)
        return 1
    if not content:
        print(f"No content= entries found in {cfg_path}", file=sys.stderr)
        return 1

    dir_cache: dict[Path, dict[str, Path]] = {}

    morrowind_path = resolve_plugin("Morrowind.esm", data_dirs, dir_cache)
    if morrowind_path is None:
        print(
            "Morrowind.esm was not found in any data directory listed in "
            f"{cfg_path}.\nCheck that your data= entries point at real folders.",
            file=sys.stderr,
        )
        return 1

    plugins: list[Path] = []
    skipped_vanilla: list[str] = []
    skipped_nonplugin: list[str] = []
    missing: list[str] = []

    for name in content:
        ext = Path(name).suffix.lower()
        if ext not in PLUGIN_EXTS:
            skipped_nonplugin.append(name)
            continue
        if Path(name).stem.lower().endswith(EQUALISED_SUFFIX):
            continue
        if name.lower() in SKIP_PLUGINS:
            skipped_vanilla.append(name)
            continue
        resolved = resolve_plugin(name, data_dirs, dir_cache)
        if resolved is None:
            missing.append(name)
            continue
        plugins.append(resolved)

    print(f"openmw.cfg:  {cfg_path}")
    print(f"tes3conv:    {tes3conv}")
    print(f"Output:      {output_dir}")
    print(f"Data dirs:   {len(data_dirs)}    Content entries: {len(content)}")
    print(f"Skipping vanilla:    {', '.join(skipped_vanilla) or '(none)'}")
    if skipped_nonplugin:
        print(f"Skipping non-plugin: {len(skipped_nonplugin)} (e.g. .omwscripts)")
    if missing:
        head = ", ".join(missing[:5])
        tail = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        print(f"Missing on disk:     {len(missing)} - {head}{tail}")
    print(f"Patching {len(plugins)} plugin(s)...\n")

    json_artifacts: list[Path] = []
    patched = 0
    no_weapons = 0
    failures: list[tuple[str, str]] = []

    for plugin in plugins:
        src_json = output_dir / f"{plugin.stem}{plugin.suffix}{DUMP_SUFFIX}"
        patch_json = output_dir / f"{plugin.stem}{EQUALISED_SUFFIX}.json"
        patch_omw = output_dir / f"{plugin.stem}{EQUALISED_SUFFIX}.omwaddon"
        json_artifacts.extend([src_json, patch_json])

        try:
            run_tes3conv(tes3conv, plugin, src_json)
        except subprocess.CalledProcessError as e:
            failures.append((plugin.name, (e.stderr or str(e)).strip()))
            continue

        try:
            with src_json.open("r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            failures.append((plugin.name, f"json load: {e}"))
            continue

        try:
            patch = build_patch(records, plugin)
        except Exception as e:
            failures.append((plugin.name, f"build: {e}"))
            continue

        if not patch:
            print(f"  - {plugin.name}: no weapons")
            no_weapons += 1
            continue

        try:
            with patch_json.open("w", encoding="utf-8") as f:
                json.dump(patch, f, indent=2)
            run_tes3conv(tes3conv, patch_json, patch_omw)
        except (OSError, subprocess.CalledProcessError) as e:
            msg = getattr(e, "stderr", None) or str(e)
            failures.append((plugin.name, f"emit: {str(msg).strip()}"))
            continue

        weapon_count = len(patch) - 1
        print(f"  + {plugin.name}: {weapon_count} weapon(s) -> {patch_omw.name}")
        patched += 1

    cleanup_jsons(json_artifacts)

    print()
    print(f"Patched: {patched}    No weapons: {no_weapons}    "
          f"Missing: {len(missing)}    Failed: {len(failures)}")
    if failures:
        print("Failures:")
        for name, msg in failures:
            print(f"  {name}: {msg}")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
