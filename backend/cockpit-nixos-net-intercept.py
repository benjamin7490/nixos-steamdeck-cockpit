#!/usr/bin/env python3
"""
cockpit-nixos-net-intercept : Intercepteur déclaratif NetworkManager -> NixOS.
Déclenché par systemd.path sur /etc/NetworkManager/system-connections/ ou par
NetworkManager dispatcher lorsqu'un administrateur modifie le réseau dans
l'onglet natif 'Networking' de Cockpit.

Intègre :
- Garde anti-boucle (/run/cockpit-nixos/rebuilding)
- Debounce de 3 secondes (évite plusieurs rebuilds lors de modifications groupées)
- Hash SHA-256 canonique (ne déclenche nixos-rebuild switch que si un profil persistant a réellement changé)
- Filtrage automatique des interfaces éphémères (Podman, Libvirt, veth, lo)
"""

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path("/var/lib/cockpit-nixos")
HASH_FILE = STATE_DIR / "nm-last-hash"
REBUILD_SENTINEL = Path("/run/cockpit-nixos/rebuilding")
CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl"

IGNORE_PREFIXES = ("lo", "podman", "cni-", "virbr", "vnet", "veth", "docker", "br-", "tun", "tap")


def split_nmcli_terse(line: str) -> list:
    return [part.replace("\\:", ":") for part in re.split(r"(?<!\\):", line)]


def snapshot_nm_profiles() -> str:
    res = subprocess.run(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"],
        capture_output=True,
        text=True,
        check=True,
    )
    items = []
    for line in sorted(res.stdout.splitlines()):
        parts = split_nmcli_terse(line)
        if len(parts) < 4:
            continue
        name, uuid, ctype, dev = parts[0], parts[1], parts[2], parts[3]
        if ctype not in ("802-3-ethernet", "ethernet", "bridge", "vlan", "bond"):
            continue
        if any(dev.startswith(p) for p in IGNORE_PREFIXES) or any(name.startswith(p) for p in IGNORE_PREFIXES):
            continue
        detail = subprocess.run(
            ["nmcli", "-t", "-f", "connection.interface-name,connection.autoconnect,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,ipv6.method", "connection", "show", uuid],
            capture_output=True,
            text=True,
            check=True,
        )
        items.append({"name": name, "type": ctype, "detail": sorted(detail.stdout.splitlines())})
    canonical = json.dumps(items, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main() -> int:
    if REBUILD_SENTINEL.exists():
        print("[net-intercept] Rebuild déjà en cours, événement ignoré (anti-boucle).", flush=True)
        return 0

    time.sleep(3)

    if REBUILD_SENTINEL.exists():
        print("[net-intercept] Rebuild détecté après debounce, événement ignoré.", flush=True)
        return 0

    try:
        current_hash = snapshot_nm_profiles()
    except Exception as exc:
        print("[net-intercept] Impossible d'interroger NetworkManager : " + str(exc), file=sys.stderr)
        return 0

    last_hash = HASH_FILE.read_text(encoding="utf-8").strip() if HASH_FILE.exists() else ""
    if current_hash == last_hash:
        print("[net-intercept] Aucun changement déclaratif NetworkManager détecté.", flush=True)
        return 0

    print("[net-intercept] Modification réseau Cockpit détectée -> synchronisation dans /etc/nixos/cockpit-managed.nix...", flush=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HASH_FILE.write_text(current_hash + "\n", encoding="utf-8")

    rc = subprocess.run([CTL_BIN, "sync-networkmanager"]).returncode
    if rc != 0:
        HASH_FILE.unlink(missing_ok=True)
        print("[net-intercept] Échec de sync-networkmanager (code " + str(rc) + ").", file=sys.stderr)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
