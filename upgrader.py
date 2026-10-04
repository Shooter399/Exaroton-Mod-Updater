#!/usr/bin/env python3
"""
upgrader.py — Upgrade every mod on an Exaroton server to a new Minecraft version.

Unlike updater.py, which updates mods *within* the current Minecraft version,
the upgrader answers one question: can this server move to `--to-version`?

Every installed mod is looked up on Modrinth and checked for a build that
supports both the target Minecraft version and the target loader. The upgrade
is only allowed when **all** mods have a compatible build — a single missing
mod blocks the whole upgrade, so the server can never end up half-upgraded.

Nothing is modified unless --upgrade is passed.

Usage:
    python upgrader.py --token <tok> --server-id <id> --to-version 1.21.1
    python upgrader.py --token <tok> --server-id <id> --to-version 1.21.1 --upgrade
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
USER_AGENT    = "exaroton-mod-upgrader/1.0"

VERSION_TYPE_SUFFIX = {
    "release": "",
    "beta":    " [BETA]",
    "alpha":   " [ALPHA]",
}

# ANSI helpers
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
CYAN   = "\033[36m"

# ── Exaroton helpers ───────────────────────────────────────────────────────────

def _exaroton_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check whether every mod supports a new Minecraft version and "
                    "optionally upgrade the server to it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Without --upgrade the script is fully read-only.",
    )
    p.add_argument("--token",
                   required=True,
                   help="Exaroton API token (generate at https://exaroton.com/account/)")
    p.add_argument("--server-id",
                   required=True,
                   help="Exaroton server ID (e.g. EwYiY9IAMtQBTb6U)")
    p.add_argument("--to-version",
                   required=True,
                   help="Minecraft version to upgrade to (e.g. 1.21.1)")
    p.add_argument("--from-version",
                   default=None,
                   help="Current Minecraft version, used for display only (optional)")
    p.add_argument("--loader",
                   default="fabric",
                   choices=["fabric", "forge", "neoforge", "quilt", "paper", "purpur"],
                   help="Mod loader to check builds for. Default: fabric")
    p.add_argument("--upgrade",
                   action="store_true",
                   help="Apply the upgrade once every mod is verified (destructive). "
                        "Prompts y/n per mod.")
    return p.parse_args()


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

def fetch_game_versions() -> set[str]:
    """
    GET /tag/game_version
    Returns the set of every Minecraft version Modrinth knows about, used to
    reject typos in --to-version before doing any work.
    """
    resp = requests.get(
        f"{MODRINTH_BASE}/tag/game_version",
        headers=_modrinth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return {v["version"] for v in resp.json()}


# ── Core workflow ──────────────────────────────────────────────────────────────

def check_upgrade_readiness(
    hash_to_filename: dict[str, str],
    current_versions: dict,
    target_versions: dict,
    loader: str,
    to_version: str,
) -> tuple[list[tuple[str, str, str, dict, dict | None]], list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Decide whether every installed mod has a build for the target MC version.

    Returns (updatable, already_ok, blocked):
        updatable  — (filename, current_ver, target_ver, target_version_obj, file_obj)
                     mods that need a new jar
        already_ok — (filename, version) mods whose installed build already supports
                     the target version
        blocked    — (filename, reason) mods with no verifiable target build

    A mod is only "ready" when Modrinth recognises the installed hash *and*
    returns a loader + game-version compatible build for it. Everything else is
    a blocker, because the upgrade must never leave a mod behind.
    """
    updatable:  list[tuple[str, str, str, dict, dict | None]] = []
    already_ok: list[tuple[str, str]] = []
    blocked:    list[tuple[str, str]] = []

    for sha1, filename in hash_to_filename.items():
        current = current_versions.get(sha1)
        if not current:
            blocked.append((filename, "not on Modrinth — availability cannot be verified"))
            print(f"  {BOLD}{filename}{RESET}: {RED}unknown mod{RESET} "
                  f"{DIM}(not on Modrinth){RESET}")
            continue

        target = target_versions.get(sha1)
        if not target:
            blocked.append((filename, f"no {loader} build for {to_version}"))
            print(f"  {BOLD}{filename}{RESET}: {RED}no {loader} build for {to_version}{RESET}")
            continue

        current_ver = current.get("version_number", current["id"])
        target_ver  = target.get("version_number", target["id"])

        if current["id"] == target["id"]:
            already_ok.append((filename, current_ver))
            print(f"  {BOLD}{filename}{RESET}: {GREEN}already supports {to_version}{RESET} "
                  f"{DIM}({current_ver}){RESET}")
        else:
            suffix     = VERSION_TYPE_SUFFIX.get(target.get("version_type", ""), "")
            suffix_fmt = f"{BOLD}{YELLOW}{suffix}{RESET}" if suffix else ""
            updatable.append((filename, current_ver, target_ver, target, primary_file_of(target)))
            print(f"  {BOLD}{filename}{RESET}: {RED}{current_ver}{RESET} -> "
                  f"{GREEN}{target_ver}{RESET}{suffix_fmt}")

    return updatable, already_ok, blocked


def print_summary_table(updatable: list) -> None:
    col = max((len(f) for f, *_ in updatable), default=4) + 2
    print(f"\n{'─' * (col + 46)}")
    print(f"  {'MOD FILE':<{col}} {'CURRENT':<18} {'TARGET':<20} TYPE")
    print(f"{'─' * (col + 46)}")
    for filename, cur_ver, target_ver, version_obj, _ in updatable:
        vtype = version_obj.get("version_type", "?")
        vtype_fmt = f"{BOLD}{YELLOW}{vtype}{RESET}" if vtype != "release" else DIM + vtype + RESET
        print(f"  {BOLD}{filename:<{col}}{RESET} {RED}{cur_ver:<18}{RESET} "
              f"{GREEN}{target_ver:<20}{RESET} {vtype_fmt}")
    print(f"{'─' * (col + 46)}")


def apply_upgrade(
    server_id: str,
    token: str,
    old_filename: str,
    target_version: dict,
    file_info: dict | None,
) -> bool:
    """
    Prompt the user, then download the target jar, upload it and delete the old
    one. Returns True if the upgrade was applied.
    """
    if not file_info:
        print(f"  [SKIP] {old_filename}: no downloadable file in the target version.")
        return False

    new_filename = file_info["filename"]
    target_ver   = target_version.get("version_number", target_version["id"])
    vtype        = target_version.get("version_type", "?")
    suffix       = VERSION_TYPE_SUFFIX.get(vtype, f" [{vtype.upper()}]")
    suffix_fmt   = f"{BOLD}{YELLOW}{suffix}{RESET}" if suffix else ""

    try:
        answer = input(
            f"{BOLD}{old_filename}{RESET}: {RED}current{RESET} -> "
            f"{GREEN}{target_ver}{RESET}{suffix_fmt} [y/N]? "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if answer != "y":
        print("  Skipped.")
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

    # 0. Reject typos in the requested Minecraft version before doing anything.
    if args.to_version not in fetch_game_versions():
        print(f"{RED}Error:{RESET} '{args.to_version}' is not a known Minecraft version.",
              file=sys.stderr)
        sys.exit(1)

    header = (f"Upgrade {args.from_version} -> {args.to_version}"
              if args.from_version else f"Target Minecraft version: {args.to_version}")
    print(f"{BOLD}{CYAN}{header}{RESET} {DIM}({args.loader}){RESET}\n")

    # 1. Download the mods directory as a ZIP and hash every .jar
    print(f"Downloading mods from server {args.server_id}...")
    hash_to_filename = download_mods_as_zip(args.server_id, args.token)

    if not hash_to_filename:
        print("No .jar files found in mods/ — nothing to upgrade.")
        return

    print(f"Found {len(hash_to_filename)} mod(s). Checking Modrinth for a "
          f"{args.loader} / {args.to_version} build of each...\n")

    # 2. Bulk-query Modrinth
    all_hashes       = list(hash_to_filename.keys())
    current_versions = fetch_current_versions(all_hashes)
    target_versions  = fetch_latest_versions(all_hashes, args.loader, args.to_version)

    # 3. Verify every single mod
    updatable, already_ok, blocked = check_upgrade_readiness(
        hash_to_filename, current_versions, target_versions, args.loader, args.to_version
    )

    # 4. The gate: never upgrade while a single mod is missing.
    if blocked:
        print(f"\n{RED}{BOLD}Cannot upgrade to {args.to_version}.{RESET}")
        print(f"{len(blocked)} of {len(hash_to_filename)} mod(s) are not available for "
              f"{args.loader} / {args.to_version}:")
        for filename, reason in blocked:
            print(f"  {RED}✗{RESET} {BOLD}{filename}{RESET}: {reason}")
        print(f"\n{DIM}Resolve the mods above (or remove them) before upgrading — "
              f"no changes were made.{RESET}")
        sys.exit(1)

    # 5. Report and (optionally) apply
    print(f"\n{GREEN}{BOLD}All {len(hash_to_filename)} mod(s) have a "
          f"{args.loader} build for {args.to_version}.{RESET}")

    if already_ok:
        print(f"{DIM}{len(already_ok)} mod(s) already support {args.to_version} "
              f"and need no change.{RESET}")

    if not updatable:
        print(f"\nNothing to upgrade — every mod already supports {args.to_version}.")
        return

    print_summary_table(updatable)
    print(f"\n{len(updatable)} mod(s) will be replaced.")

    if not args.upgrade:
        print("Run with --upgrade to apply this upgrade.")
        return

    print()
    applied = sum(
        apply_upgrade(args.server_id, args.token, old_fn, ver_obj, file_info)
        for old_fn, _cur, _target, ver_obj, file_info in updatable
    )
    print(f"\n{applied}/{len(updatable)} mod(s) upgraded to {args.to_version}.")


if __name__ == "__main__":
    main()