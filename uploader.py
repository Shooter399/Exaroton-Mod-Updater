#\!/usr/bin/env python3
"""
uploader.py — Upload new mods from a local profile directory to an Exaroton server.

Reads fabric.mod.json from each local .jar to determine the mod's target environment.
Only mods with environment "*" or "server" are candidates for upload.
Mods whose mod ID is already present on the server are skipped.

Usage:
    python uploader.py --token <tok> --server-id <id> --path <dir>
    python uploader.py --token <tok> --server-id <id> --path <dir> --upload
"""

import argparse
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import json5
import requests

EXAROTON_BASE = "https://api.exaroton.com/v1"
MODS_PATH     = "mods"
USER_AGENT    = "exaroton-mod-uploader/1.0"

# ANSI helpers
BOLD  = "\033[1m"
DIM   = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED   = "\033[31m"


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Upload new mods from a local directory to an Exaroton server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Without --upload the script is fully read-only.",
    )
    p.add_argument("--token",
                   required=True,
                   help="Exaroton API token (generate at https://exaroton.com/account/)")
    p.add_argument("--server-id",
                   required=True,
                   help="Exaroton server ID (e.g. EwYiY9IAMtQBTb6U)")
    p.add_argument("--path",
                   required=True,
                   type=Path,
                   help="Path to the local mods directory")
    p.add_argument("--upload",
                   action="store_true",
                   help="Perform uploads. Without this flag only a check is done.")
    return p.parse_args()


# ── Exaroton helpers ───────────────────────────────────────────────────────────

def _exaroton_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_server_mod_ids(server_id: str, token: str) -> set[str]:
    """
    Download the server mods as a ZIP and return the set of mod IDs found
    in each jar's fabric.mod.json.
    """
    resp = requests.get(
        f"{EXAROTON_BASE}/servers/{server_id}/files/data/{MODS_PATH}",
        headers=_exaroton_headers(token),
        timeout=120,
    )
    resp.raise_for_status()

    mod_ids: set[str] = set()
    with zipfile.ZipFile(BytesIO(resp.content)) as outer_zip:
        for entry in outer_zip.namelist():
            if not entry.endswith(".jar"):
                continue
            try:
                with zipfile.ZipFile(BytesIO(outer_zip.read(entry))) as jar:
                    if "fabric.mod.json" not in jar.namelist():
                        continue
                    with jar.open("fabric.mod.json") as f:
                        mod_id = json5.load(f, strict=False).get("id")
                        if mod_id:
                            mod_ids.add(mod_id)
            except Exception:
                pass  # skip corrupted or non-Fabric jars silently

    return mod_ids


def upload_mod(server_id: str, token: str, filename: str, data: bytes) -> None:
    resp = requests.put(
        f"{EXAROTON_BASE}/servers/{server_id}/files/data/{MODS_PATH}/{filename}",
        headers={**_exaroton_headers(token), "Content-Type": "application/octet-stream"},
        data=data,
        timeout=120,
    )
    resp.raise_for_status()


# ── Local mod scanning ─────────────────────────────────────────────────────────

def read_local_mods(directory: Path) -> list[tuple[Path, str, str]]:
    """
    Scan directory for .jar files and read fabric.mod.json from each.
    Returns a list of (jar_path, mod_id, environment) tuples.
    Jars without a readable fabric.mod.json are printed and skipped.
    """
    results: list[tuple[Path, str, str]] = []
    for jar_path in sorted(directory.glob("*.jar")):
        if not jar_path.is_file(follow_symlinks=False):
            continue
        try:
            with zipfile.ZipFile(jar_path) as jar:
                if "fabric.mod.json" not in jar.namelist():
                    print(f"  {DIM}{jar_path.name}: no fabric.mod.json — skipped{RESET}")
                    continue
                with jar.open("fabric.mod.json") as f:
                    meta = json5.load(f, strict=False)
        except Exception as exc:
            print(f"  {DIM}{jar_path.name}: unreadable ({exc}) — skipped{RESET}")
            continue

        mod_id      = meta.get("id", "")
        environment = meta.get("environment", "*")
        results.append((jar_path, mod_id, environment))

    return results


# ── Core workflow ──────────────────────────────────────────────────────────────

def find_uploads(
    local_mods: list[tuple[Path, str, str]],
    server_mod_ids: set[str],
) -> list[tuple[Path, str]]:
    """
    Filter local mods down to those that should be uploaded.
    Prints a status line for every mod and returns (jar_path, mod_id) pairs.
    """
    to_upload: list[tuple[Path, str]] = []

    for jar_path, mod_id, environment in local_mods:
        if environment == "client":
            print(f"  {DIM}{jar_path.name}: client-only — skipped{RESET}")
            continue
        if mod_id in server_mod_ids:
            print(f"  {BOLD}{jar_path.name}{RESET}: already on server {DIM}({mod_id}){RESET}")
            continue
        to_upload.append((jar_path, mod_id))
        print(f"  {BOLD}{jar_path.name}{RESET}: {GREEN}will upload{RESET} {DIM}({mod_id}){RESET}")

    return to_upload


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    if not args.path.is_dir():
        print(f"{RED}Error:{RESET} '{args.path}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # 1. Get mod IDs already on the server
    print(f"Fetching mods from server {args.server_id}...")
    server_mod_ids = fetch_server_mod_ids(args.server_id, args.token)
    print(f"Server has {len(server_mod_ids)} mod(s) installed.\n")

    # 2. Scan local directory
    print(f"Scanning {args.path}")
    local_mods = read_local_mods(args.path)

    # 3. Determine what needs uploading
    to_upload = find_uploads(local_mods, server_mod_ids)

    if not to_upload:
        print("\nNothing to upload.")
        return

    print(f"\n{len(to_upload)} mod(s) to upload.")

    if not args.upload:
        print("Run with --upload to apply these changes.")
        return

    # 4. Upload
    print()
    uploaded = 0
    for jar_path, mod_id in to_upload:
        print(f"  Uploading {BOLD}{jar_path.name}{RESET}...", end=" ", flush=True)
        try:
            upload_mod(args.server_id, args.token, jar_path.name, jar_path.read_bytes())
            print(f"{GREEN}done{RESET}.")
            uploaded += 1
        except requests.HTTPError as exc:
            print(f"{RED}failed{RESET} ({exc}).")

    print(f"\n{uploaded}/{len(to_upload)} mod(s) uploaded.")


if __name__ == "__main__":
    main()
