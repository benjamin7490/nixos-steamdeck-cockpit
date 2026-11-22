#!/usr/bin/env python3
"""
cockpit-nixos-storage-intercept : Intercepteur déclaratif Stockage / Disques
pour le VRAI onglet natif 'Stockage' (cockpit-storaged / udisks2) de Cockpit.

Surveille à la fois :
  1. /etc/fstab (modifié directement par udisks2 et cockpit-storaged pour les
     disques locaux, partitions, LUKS, LVM, RAID et partages NFS) ;
  2. /proc/self/mountinfo (événementiel via select.poll sur POLLPRI|POLLERR)
     pour les montages effectués à chaud sous /mnt/, /srv/, /media/ ou /data/.

Comportement sûr sur NixOS :
  - Mémorise au premier démarrage les montages statiques préexistants issus de
    hardware-configuration.nix / configuration.nix afin de ne jamais les modifier
    ni créer de conflit NixOS.
  - Résout UUID=, LABEL=, PARTUUID= et /dev/sdX en chemins persistants
    (/dev/disk/by-uuid/...) via blkid.
  - Démonter temporairement un disque encore présent dans /etc/fstab ne le
    supprime pas de /etc/nixos/cockpit-managed.nix.
  - Synchronise de manière atomique via `cockpit-nixos-ctl mounts-sync` puis
    `nixos-rebuild switch`, ce qui régénère proprement /etc/fstab.
"""

import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path("/var/lib/cockpit-nixos")
STATE_FILE = STATE_DIR / "state.json"
HASH_FILE = STATE_DIR / "storage-last-hash"
STATIC_MOUNTS_FILE = STATE_DIR / "static-mounts.json"
REBUILD_SENTINEL = Path("/run/cockpit-nixos/rebuilding")
FSTAB_PATH = Path("/etc/fstab")
CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl"

MANAGED_PREFIXES = ("/mnt/", "/srv/", "/media/", "/data/")
ALLOWED_FSTYPES = {"ext4", "xfs", "btrfs", "vfat", "ntfs", "ntfs3", "fuseblk", "nfs", "nfs4", "cifs", "auto"}
SAFE_MOUNTINFO_OPTS = {"rw", "ro", "noatime", "relatime", "nodiratime", "discard", "compress=zstd", "nofail", "x-systemd.automount"}
OPT_RE = re.compile(r"^[a-zA-Z0-9_=,./:@-]+$")


def unescape_mount_path(raw: str) -> str:
    return (
        raw.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def fstab_signature() -> tuple:
    try:
        st = FSTAB_PATH.stat()
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0, 0)


def resolve_stable_device(source: str, fs_type: str) -> str:
    src = source.strip()
    if src.startswith("UUID="):
        return "/dev/disk/by-uuid/" + src.split("=", 1)[1].strip('"')
    if src.startswith("LABEL="):
        return "/dev/disk/by-label/" + src.split("=", 1)[1].strip('"')
    if src.startswith("PARTUUID="):
        return "/dev/disk/by-partuuid/" + src.split("=", 1)[1].strip('"')
    if fs_type in ("nfs", "nfs4", "cifs") or not src.startswith("/dev/") or src.startswith("/dev/disk/by-"):
        return src
    try:
        res = subprocess.run(["blkid", "-s", "UUID", "-o", "value", src], capture_output=True, text=True, timeout=5)
        uuid = res.stdout.strip()
        if uuid and Path("/dev/disk/by-uuid/" + uuid).exists():
            return "/dev/disk/by-uuid/" + uuid
    except Exception:
        pass
    return src


def normalize_fstype(fs_type: str) -> str:
    ft = fs_type.strip()
    if ft in ("fuseblk", "ntfs3"):
        return "ntfs"
    return ft


def parse_fstab_mounts(static_mps: set) -> dict:
    by_mp = {}
    if not FSTAB_PATH.exists():
        return by_mp
    try:
        lines = FSTAB_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return by_mp

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        spec = unescape_mount_path(parts[0])
        mp = os.path.normpath(unescape_mount_path(parts[1]))
        fs_type = normalize_fstype(parts[2])
        opts_raw = parts[3]

        if not mp.startswith(MANAGED_PREFIXES) or mp in static_mps:
            continue
        if any(c in mp for c in (" ", "\t", "\n", '"', "\\")):
            continue
        if fs_type not in ALLOWED_FSTYPES:
            continue

        opts = [o.strip() for o in opts_raw.split(",") if o.strip() and OPT_RE.match(o.strip())]
        if not opts:
            opts = ["defaults"]
        if "nofail" not in opts and "noauto" not in opts:
            opts.append("nofail")
        if fs_type in ("nfs", "nfs4", "cifs") and "_netdev" not in opts:
            opts.append("_netdev")

        dev = resolve_stable_device(spec, fs_type)
        by_mp[mp] = {
            "mountPoint": mp,
            "device": dev,
            "fsType": fs_type,
            "options": sorted(set(opts)),
        }
    return by_mp


def load_static_mounts() -> set:
    if STATIC_MOUNTS_FILE.exists():
        try:
            return set(json.loads(STATIC_MOUNTS_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()

    # Première exécution : tout montage sous MANAGED_PREFIXES déjà présent dans
    # /etc/fstab mais absent de state.json est un montage statique de configuration.nix
    managed_in_state = set()
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            managed_in_state = {m.get("mountPoint", "") for m in st.get("mounts", [])}
        except Exception:
            pass

    fstab_all = parse_fstab_mounts(set())
    static_mps = sorted(mp for mp in fstab_all.keys() if mp not in managed_in_state)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_MOUNTS_FILE.write_text(json.dumps(static_mps, indent=2) + "\n", encoding="utf-8")
    return set(static_mps)


def read_managed_mounts(static_mps: set) -> list:
    # Priorité 1 : entrées /etc/fstab (écrites par udisks2 / cockpit-storaged)
    by_mp = parse_fstab_mounts(static_mps)

    # Priorité 2 : montages actifs dans /proc/self/mountinfo non encore présents dans fstab
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    for line in lines:
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        lparts = left.split()
        rparts = right.split()
        if len(lparts) < 6 or len(rparts) < 2:
            continue
        mp = os.path.normpath(unescape_mount_path(lparts[4]))
        if not mp.startswith(MANAGED_PREFIXES) or mp in static_mps or mp in by_mp:
            continue
        if any(c in mp for c in (" ", "\t", "\n", '"', "\\")):
            continue
        fs_type = normalize_fstype(rparts[0])
        source = unescape_mount_path(rparts[1])
        if fs_type not in ALLOWED_FSTYPES:
            continue

        raw_opts = (lparts[5] + "," + (rparts[2] if len(rparts) > 2 else "")).split(",")
        opts = ["defaults", "nofail"]
        for o in raw_opts:
            if o in SAFE_MOUNTINFO_OPTS and o not in opts:
                opts.append(o)
        if fs_type in ("nfs", "nfs4", "cifs") and "_netdev" not in opts:
            opts.append("_netdev")

        dev = resolve_stable_device(source, fs_type)
        by_mp[mp] = {
            "mountPoint": mp,
            "device": dev,
            "fsType": fs_type,
            "options": sorted(set(opts)),
        }

    return sorted(by_mp.values(), key=lambda m: m["mountPoint"])


def sync_if_changed(static_mps: set) -> None:
    if REBUILD_SENTINEL.exists():
        return
    mounts = read_managed_mounts(static_mps)
    canonical = json.dumps(mounts, sort_keys=True)
    current_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    last_hash = HASH_FILE.read_text(encoding="utf-8").strip() if HASH_FILE.exists() else ""
    if current_hash == last_hash:
        return

    print("[storage-intercept] Changement Stockage/fstab détecté -> synchronisation déclarative NixOS...", flush=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HASH_FILE.write_text(current_hash + "\n", encoding="utf-8")
    rc = subprocess.run([CTL_BIN, "mounts-sync", canonical]).returncode
    if rc != 0:
        HASH_FILE.unlink(missing_ok=True)
        print("[storage-intercept] Échec de mounts-sync (code " + str(rc) + ").", file=sys.stderr, flush=True)


def main() -> int:
    static_mps = load_static_mounts()
    if not HASH_FILE.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        initial = json.dumps(read_managed_mounts(static_mps), sort_keys=True)
        HASH_FILE.write_text(hashlib.sha256(initial.encode("utf-8")).hexdigest() + "\n", encoding="utf-8")

    last_fstab_sig = fstab_signature()
    print("[storage-intercept] Surveillance /etc/fstab + /proc/self/mountinfo active.", flush=True)

    with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
        poller = select.poll()
        poller.register(fh.fileno(), select.POLLPRI | select.POLLERR)
        while True:
            events = poller.poll(2000)
            cur_fstab_sig = fstab_signature()
            fstab_changed = (cur_fstab_sig != last_fstab_sig)
            if not events and not fstab_changed:
                continue

            if events:
                fh.seek(0)
                fh.read()

            # Debounce de 3 secondes (laisse udisks2/cockpit-storaged terminer l'opération)
            time.sleep(3)
            fh.seek(0)
            fh.read()
            sync_if_changed(static_mps)
            last_fstab_sig = fstab_signature()
    return 0


if __name__ == "__main__":
    sys.exit(main())
