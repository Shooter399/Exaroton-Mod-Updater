#!/usr/bin/env python3
"""
update_mods.py — Check and optionally update mods on an Exaroton server.

Downloads the mods/ directory as a ZIP, hashes every .jar, cross-references
against Modrinth and reports available updates. Nothing is modified unless
--update is passed.

Usage:
    python update_mods.py --token <tok> --server-id <id> --game-version 26.2
    python update_mods.py --token <tok> --server-id <id> --game-version 26.2 --update
"""

import argparse
import hashlib
import sys
import zipfile
from io import BytesIO

import requests

EXAROTON_BASE = "https://api.exaroton.com/v1"
MODRINTH_BASE = "https://api.modrinth.com/v2"
MODS_PATH     = "mods"
USER_AGENT    = "exaroton-mod-updater/1.0"

VERSION_TYPE_SUFFIX = {
    "release": "",
    "beta":    " [BETA]",
    "alpha":   " [ALPHA]", 
}

# ANSI helpers
BOLD  = "\033[1m"
DIM   = "\033[2m"
RESET = "\033[0m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
CYAN   = "\033[36m"


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check and optionally update mods on an Exaroton Minecraft server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Without --update the script is fully read-only.",
    )
    p.add_argument("--token",
                   required=True,
                   help="Exaroton API token (generate at https://exaroton.com/account/)")
    p.add_argument("--server-id",
                   required=True,
                   help="Exaroton server ID (e.g. EwYiY9IAMtQBTb6U)")
    p.add_argument("--game-version",
                   required=True,
                   help="Target Minecraft version (e.g. 26.2, 1.21.1)")
    p.add_argument("--loader",
                   default="fabric",
                   choices=["fabric", "forge", "neoforge", "quilt", "paper", "purpur"],
                   help="Mod loader to filter updates for. Default: fabric")
    p.add_argument("--update",
                   action="store_true",
                   help="Apply available updates (destructive). Prompts y/n per mod.")
    return p.parse_args()


# ── Exaroton helpers ───────────────────────────────────────────────────────────

def _exaroton_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def download_mods_as_zip(server_id: str, token: str) -> dict[str, str]:
    """
    GET /servers/{server}/files/data/mods  (returns the directory as a ZIP)

    Returns a dict mapping  sha1_hash -> filename  for every .jar in the archive.
    """
    resp = requests.get(
        f"{EXAROTON_BASE}/servers/{server_id}/files/data/{MODS_PATH}",
        headers=_exaroton_headers(token),
        timeout=120,
    )
    resp.raise_for_status()

    hash_to_filename: dict[str, str] = {}
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        for entry in zf.namelist():
            if not entry.endswith(".jar"):
                continue
            with zf.open(entry) as jar_file:
                jar_bytes = jar_file.read()
            sha1 = hashlib.sha1(jar_bytes).hexdigest()
            # Strip directory prefix (e.g. "mods/mod.jar" -> "mod.jar")
            filename = entry.split("/")[-1]
            hash_to_filename[sha1] = filename

    return hash_to_filename


def upload_mod(server_id: str, token: str, filename: str, data: bytes) -> None:
    """PUT /servers/{server}/files/data/mods/{filename}"""
    resp = requests.put(
        f"{EXAROTON_BASE}/servers/{server_id}/files/data/{MODS_PATH}/{filename}",
        headers={**_exaroton_headers(token), "Content-Type": "application/octet-stream"},
        data=data,
        timeout=120,
    )
    resp.raise_for_status()


def delete_mod(server_id: str, token: str, filename: str) -> None:
    """DELETE /servers/{server}/files/data/mods/{filename}"""
    resp = requests.delete(
        f"{EXAROTON_BASE}/servers/{server_id}/files/data/{MODS_PATH}/{filename}",
        headers=_exaroton_headers(token),
        timeout=15,
    )
    resp.raise_for_status()


# ── Modrinth helpers ───────────────────────────────────────────────────────────

def _modrinth_headers() -> dict:
    return {"User-Agent": USER_AGENT}


def fetch_current_versions(hashes: list[str]) -> dict:
    """
    POST /version_files
    Returns {sha1_hash: version_object} for every hash recognised by Modrinth.
    Hashes not found on Modrinth are absent from the result.
    """
    resp = requests.post(
        f"{MODRINTH_BASE}/version_files",
        headers=_modrinth_headers(),
        json={"hashes": hashes, "algorithm": "sha1"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_latest_versions(hashes: list[str], loader: str, game_version: str) -> dict:
    """
    POST /version_files/update
    Returns {sha1_hash: latest_version_object} filtered by loader + game version.
    Hashes with no compatible update are absent from the result.
    """
    resp = requests.post(
        f"{MODRINTH_BASE}/version_files/update",
        headers=_modrinth_headers(),
        json={
            "hashes": hashes,
            "algorithm": "sha1",
            "loaders": [loader],
            "game_versions": [game_version],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def primary_file_of(version: dict) -> dict | None:
    """Return the primary download file entry from a Modrinth version object."""
    files = version.get("files") or []
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def download_file_from_url(url: str) -> bytes:
    resp = requests.get(url, headers=_modrinth_headers(), timeout=120)
    resp.raise_for_status()
    return resp.content


# ── Core workflow ──────────────────────────────────────────────────────────────

def find_updates(
    hash_to_filename: dict[str, str],
    current_versions: dict,
    latest_versions: dict,
) -> list[tuple[str, str, str, dict, dict | None]]:
    """
    Compare installed versions against the latest available on Modrinth.

    Returns a list of tuples for mods that can be updated:
        (old_filename, current_version_str, latest_version_str,
         latest_version_object, primary_file_object)

    Also prints a one-line status for every mod.
    """
    updates: list[tuple[str, str, str, dict, dict | None]] = []

    for sha1, filename in hash_to_filename.items():
        current = current_versions.get(sha1)
        if not current:
            print(f"  {filename}: not found on Modrinth — skipped")
            continue

        latest = latest_versions.get(sha1)
        if not latest:
            print(f"  {filename}: no compatible version for this loader/MC — skipped")
            continue

        current_ver = current.get("version_number", current["id"])
        latest_ver  = latest.get("version_number",  latest["id"])

        if current["id"] == latest["id"]:
            print(f"  {BOLD}{filename}{RESET}: up to date {DIM}({current_ver}){RESET}")
        else:
            vtype_suffix = VERSION_TYPE_SUFFIX.get(latest.get("version_type", ""), "")
            vtype_colored = f"{BOLD}{YELLOW}{vtype_suffix}{RESET}" if vtype_suffix else ""
            updates.append((filename, current_ver, latest_ver, latest, primary_file_of(latest)))
            print(f"  {BOLD}{filename}{RESET}: {RED}{current_ver}{RESET} -> {GREEN}{latest_ver}{RESET}{vtype_colored}")

    return updates


def print_summary_table(updates: list) -> None:
    col = max(len(f) for f, *_ in updates) + 2
    print(f"\n{'─' * (col + 46)}")
    print(f"  {'MOD FILE':<{col}} {'CURRENT':<18} {'LATEST':<20} TYPE")
    print(f"{'─' * (col + 46)}")
    for filename, cur_ver, latest_ver, version_obj, _ in updates:
        vtype = version_obj.get("version_type", "?")
        vtype_fmt = f"{BOLD}{YELLOW}{vtype}{RESET}" if vtype != "release" else DIM + vtype + RESET
        # pad before injecting colour codes so column widths stay correct
        print(f"  {BOLD}{filename:<{col}}{RESET} {RED}{cur_ver:<18}{RESET} {GREEN}{latest_ver:<20}{RESET} {vtype_fmt}")
    print(f"{'─' * (col + 46)}")


def apply_update(
    server_id: str,
    token: str,
    old_filename: str,
    latest_version: dict,
    file_info: dict | None,
) -> bool:
    """
    Prompt the user, then download the new jar, upload it and delete the old one.
    Returns True if the update was applied.
    """
    if not file_info:
        print(f"  [SKIP] {old_filename}: no downloadable file in latest version.")
        return False

    latest_ver   = latest_version.get("version_number", latest_version["id"])
    vtype        = latest_version.get("version_type", "?")
    vtype_suffix = VERSION_TYPE_SUFFIX.get(vtype, f" [{vtype.upper()}]")
    new_filename = file_info["filename"]

    vtype_fmt = f"{BOLD}{YELLOW}{vtype_suffix}{RESET}" if vtype_suffix else ""
    try:
        answer = input(
            f"{BOLD}{old_filename}{RESET}: {RED}current{RESET} -> {GREEN}{latest_ver}{RESET}{vtype_fmt} [y/N]? "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if answer != "y":
        print(f"  Skipped.")
        return False

    print(f"  Downloading {new_filename}...", end=" ", flush=True)
    new_data = download_file_from_url(file_info["url"])
    print("uploading...", end=" ", flush=True)
    upload_mod(server_id, token, new_filename, new_data)
    if new_filename != old_filename:
        print("removing old...", end=" ", flush=True)
        delete_mod(server_id, token, old_filename)
    print("done.")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # 1. Download the mods directory as a ZIP and hash every .jar
    print(f"Downloading mods from server {args.server_id}...")
    hash_to_filename = download_mods_as_zip(args.server_id, args.token)

    if not hash_to_filename:
        print("No .jar files found in mods/.")
        sys.exit(0)

    print(f"Found {len(hash_to_filename)} mod(s). Querying Modrinth for"
          f" {args.loader} / {args.game_version}...\n")

    # 2. Bulk-query Modrinth
    all_hashes       = list(hash_to_filename.keys())
    current_versions = fetch_current_versions(all_hashes)
    latest_versions  = fetch_latest_versions(all_hashes, args.loader, args.game_version)

    # 3. Diff installed vs available
    updates = find_updates(hash_to_filename, current_versions, latest_versions)

    if not updates:
        print("\nAll mods are up to date.")
        return

    # 4. Summary table
    print_summary_table(updates)
    print(f"\n{len(updates)} update(s) available.")

    if not args.update:
        print("Run with --update to apply these changes.")
        return

    # 5. Per-mod y/n prompt and apply
    print()
    applied = sum(
        apply_update(args.server_id, args.token, old_fn, ver_obj, file_info)
        for old_fn, _cur, _lat, ver_obj, file_info in updates
    )
    print(f"\n{applied}/{len(updates)} update(s) applied.")


if __name__ == "__main__":
    main()
