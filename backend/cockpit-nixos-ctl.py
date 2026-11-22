#!/usr/bin/env python3
"""
cockpit-nixos-ctl : Backend privilégié transactionnel pour le plugin Cockpit NixOS.
Gère l'état structuré (/var/lib/cockpit-nixos/state.json), génère de manière atomique
/etc/nixos/cockpit-managed.nix, valide la syntaxe via nix-instantiate --parse,
exécute nixos-rebuild switch avec rollback automatique en cas d'erreur,
et gère générations, rollbacks, garbage collection et télémétrie hwmon/Steam Deck.
"""

import fcntl
import glob
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/var/lib/cockpit-nixos")
STATE_FILE = STATE_DIR / "state.json"
UPDATES_CACHE = STATE_DIR / "pk-updates-cache.json"
BACKUP_DIR = STATE_DIR / "backups"
MANAGED_NIX = Path("/etc/nixos/cockpit-managed.nix")
LOCK_FILE = Path("/run/lock/cockpit-nixos.lock")
REBUILD_SENTINEL = Path("/run/cockpit-nixos/rebuilding")
LAST_JOB_LOG = Path("/run/cockpit-nixos/last-job.log")
SYSTEM_PROFILE = Path("/nix/var/nix/profiles/system")

PKG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]*(?:\.[a-zA-Z0-9][a-zA-Z0-9._+-]*)*$")
HOST_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
IFACE_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,15}$")
OPT_RE = re.compile(r"^[a-zA-Z0-9_=,./:@-]+$")
DEV_RE = re.compile(r"^[a-zA-Z0-9_./:@=+-]+$")
NM_KEY_RE = re.compile(r"^[a-zA-Z0-9._ -]{1,64}$")
CALENDAR_RE = re.compile(r"^[a-zA-Z0-9:*.,/_ -]{1,64}$")
HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

ALLOWED_FSTYPES = {"ext4", "xfs", "btrfs", "vfat", "ntfs", "nfs", "nfs4", "cifs", "tmpfs", "none", "auto"}
ALLOWED_MOUNT_PREFIXES = ("/mnt/", "/srv/", "/media/", "/data/", "/var/lib/libvirt/images", "/var/lib/containers")
FORBIDDEN_MOUNTS = {"/", "/boot", "/etc", "/nix", "/nix/store", "/usr", "/var", "/proc", "/sys", "/dev", "/run"}

DEFAULT_STATE = {
    "packages": [],
    "firewall": {
        "enable": True,
        "allowedTCPPorts": [],
        "allowedUDPPorts": [],
        "trustedInterfaces": [],
    },
    "network": {
        "hostname": "",
        "nameservers": [],
        "nmProfiles": {},
    },
    "mounts": [],
    "automation": {},
}


def nix_str(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$" + "{", "\\$" + "{")
    )
    return '"' + escaped + '"'


def load_state() -> dict:
    if not STATE_FILE.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    with STATE_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged["packages"] = list(dict.fromkeys(data.get("packages", [])))
    if isinstance(data.get("firewall"), dict):
        merged["firewall"].update(data["firewall"])
    if isinstance(data.get("network"), dict):
        merged["network"].update(data["network"])
    if isinstance(data.get("mounts"), list):
        merged["mounts"] = data["mounts"]
    if isinstance(data.get("automation"), dict):
        merged["automation"] = data["automation"]
    return merged


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)


def render_managed_nix(state: dict) -> str:
    lines = [
        "# =============================================================================",
        "# FICHIER GÉRÉ AUTOMATIQUEMENT PAR COCKPIT-NIXOS (/etc/nixos/cockpit-managed.nix)",
        "# Ne pas éditer manuellement : vos changements seraient écrasés lors de la",
        "# prochaine opération depuis Cockpit. Utilisez /etc/nixos/configuration.nix",
        "# pour votre configuration statique personnalisée.",
        "# Généré le : " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "# =============================================================================",
        "{ config, pkgs, lib, ... }:",
        "{",
    ]

    pkgs = sorted(set(state.get("packages", [])))
    if pkgs:
        lines.append("  environment.systemPackages = with pkgs; [")
        for p in pkgs:
            lines.append("    " + p)
        lines.append("  ];")
        lines.append("")

    fw = state.get("firewall", {})
    tcp_ports = sorted(set(int(p) for p in fw.get("allowedTCPPorts", [])))
    udp_ports = sorted(set(int(p) for p in fw.get("allowedUDPPorts", [])))
    trusted = sorted(set(fw.get("trustedInterfaces", [])))
    if fw.get("enable") is False or tcp_ports or udp_ports or trusted:
        lines.append("  networking.firewall = {")
        if fw.get("enable") is False:
            lines.append("    enable = lib.mkForce false;")
        if tcp_ports:
            lines.append("    allowedTCPPorts = [ " + " ".join(str(p) for p in tcp_ports) + " ];")
        if udp_ports:
            lines.append("    allowedUDPPorts = [ " + " ".join(str(p) for p in udp_ports) + " ];")
        if trusted:
            lines.append("    trustedInterfaces = [ " + " ".join(nix_str(i) for i in trusted) + " ];")
        lines.append("  };")
        lines.append("")

    net = state.get("network", {})
    hostname = (net.get("hostname") or "").strip()
    if hostname:
        lines.append("  networking.hostName = lib.mkForce " + nix_str(hostname) + ";")
    nameservers = [ns.strip() for ns in net.get("nameservers", []) if ns.strip()]
    if nameservers:
        lines.append("  networking.nameservers = lib.mkForce [ " + " ".join(nix_str(ns) for ns in nameservers) + " ];")

    nm_profiles = net.get("nmProfiles", {})
    if nm_profiles:
        lines.append("")
        lines.append("  networking.networkmanager.ensureProfiles.profiles = {")
        for pname in sorted(nm_profiles.keys()):
            prof = nm_profiles[pname]
            lines.append("    " + nix_str(pname) + " = {")
            for section in sorted(prof.keys()):
                sec_dict = prof[section]
                if not isinstance(sec_dict, dict) or not sec_dict:
                    continue
                lines.append("      " + nix_str(section) + " = {")
                for k in sorted(sec_dict.keys()):
                    v = sec_dict[k]
                    if isinstance(v, bool):
                        val_nix = "true" if v else "false"
                    else:
                        val_nix = nix_str(str(v))
                    lines.append("        " + nix_str(k) + " = " + val_nix + ";")
                lines.append("      };")
            lines.append("    };")
        lines.append("  };")

    mounts = state.get("mounts", [])
    if mounts:
        lines.append("")
        lines.append("  fileSystems = {")
        for m in mounts:
            mp = m["mountPoint"]
            lines.append("    " + nix_str(mp) + " = {")
            lines.append("      device = lib.mkDefault " + nix_str(m["device"]) + ";")
            lines.append("      fsType = lib.mkDefault " + nix_str(m["fsType"]) + ";")
            opts = m.get("options", [])
            if opts:
                lines.append("      options = lib.mkDefault [ " + " ".join(nix_str(o) for o in opts) + " ];")
            lines.append("    };")
        lines.append("  };")

    auto = state.get("automation", {})
    if isinstance(auto, dict):
        au = auto.get("autoUpgrade")
        if isinstance(au, dict) and "enable" in au:
            lines.append("")
            lines.append("  system.autoUpgrade = {")
            lines.append("    enable = lib.mkForce " + ("true" if au.get("enable") else "false") + ";")
            if au.get("dates"):
                lines.append("    dates = lib.mkForce " + nix_str(au["dates"]) + ";")
            if "allowReboot" in au:
                lines.append("    allowReboot = lib.mkForce " + ("true" if au.get("allowReboot") else "false") + ";")
            if au.get("allowReboot") and au.get("rebootLower") and au.get("rebootUpper"):
                lines.append("    rebootWindow = lib.mkForce {")
                lines.append("      lower = " + nix_str(au["rebootLower"]) + ";")
                lines.append("      upper = " + nix_str(au["rebootUpper"]) + ";")
                lines.append("    };")
            elif au.get("allowReboot") is False or au.get("useRebootWindow") is False:
                lines.append("    rebootWindow = lib.mkForce null;")
            lines.append("  };")

        so = auto.get("storeOptimise")
        if isinstance(so, dict):
            if "autoOptimiseStore" in so:
                lines.append("")
                lines.append("  nix.settings.auto-optimise-store = lib.mkForce " + ("true" if so.get("autoOptimiseStore") else "false") + ";")
            if "periodicEnable" in so:
                lines.append("  nix.optimise = {")
                lines.append("    automatic = lib.mkForce " + ("true" if so.get("periodicEnable") else "false") + ";")
                if so.get("periodicDates"):
                    lines.append("    dates = lib.mkForce [ " + nix_str(so["periodicDates"]) + " ];")
                lines.append("  };")

        gc = auto.get("gc")
        if isinstance(gc, dict) and "automatic" in gc:
            lines.append("")
            lines.append("  nix.gc = {")
            lines.append("    automatic = lib.mkForce " + ("true" if gc.get("automatic") else "false") + ";")
            if gc.get("dates"):
                lines.append("    dates = lib.mkForce " + nix_str(gc["dates"]) + ";")
            ret = gc.get("retention", "14d")
            gc_opt = "--delete-old" if ret == "all" else ("--delete-older-than " + ret)
            lines.append("    options = lib.mkForce " + nix_str(gc_opt) + ";")
            lines.append("  };")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


_STDOUT_CLOSED = False


def safe_log(text: str) -> None:
    """Écrit sur stdout sans jamais lever BrokenPipeError si Cockpit se recharge
    pendant switch-to-configuration (ex. reload dbus-broker/polkit), et journalise
    aussi dans /run/cockpit-nixos/last-job.log."""
    global _STDOUT_CLOSED
    try:
        LAST_JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LAST_JOB_LOG.open("a", encoding="utf-8") as log_fh:
            log_fh.write(text)
    except OSError:
        pass
    if not _STDOUT_CLOSED:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            _STDOUT_CLOSED = True


def stream_cmd(cmd: list, check: bool = True, cwd: str | None = None) -> int:
    safe_log("$ " + " ".join(cmd) + "\n")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        safe_log(line)
    rc = proc.wait()
    if check and rc != 0:
        raise RuntimeError("Commande échouée (code " + str(rc) + ") : " + " ".join(cmd))
    return rc


def with_lock(fn):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def ensure_nix_env() -> None:
    path = os.environ.get("PATH", "")
    sw_bin = "/run/current-system/sw/bin"
    if sw_bin not in path.split(":"):
        os.environ["PATH"] = sw_bin + ":/run/wrappers/bin:" + path
    if not os.environ.get("NIX_PATH"):
        root_channels = "/nix/var/nix/profiles/per-user/root/channels"
        for chan in ("nixos", "nixpkgs"):
            if Path(root_channels + "/" + chan).exists():
                os.environ["NIX_PATH"] = (
                    "nixpkgs=" + root_channels + "/" + chan
                    + ":nixos-config=/etc/nixos/configuration.nix"
                    + ":" + root_channels
                )
                break


def ensure_root() -> None:
    ensure_nix_env()
    if os.geteuid() != 0:
        raise PermissionError("Privilèges root requis (exécutez via sudo ou Cockpit Administrative Access).")


def apply_state_transactional(new_state: dict, mode: str = "switch") -> None:
    ensure_root()

    def _run():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        REBUILD_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        old_nix_backup = BACKUP_DIR / ("cockpit-managed-" + ts + ".nix")
        old_state_backup = BACKUP_DIR / ("state-" + ts + ".json")

        had_old_nix = MANAGED_NIX.exists()
        had_old_state = STATE_FILE.exists()
        if had_old_nix:
            shutil.copy2(MANAGED_NIX, old_nix_backup)
        if had_old_state:
            shutil.copy2(STATE_FILE, old_state_backup)

        nix_content = render_managed_nix(new_state)
        tmp_nix = Path("/etc/nixos/.cockpit-managed.nix.tmp")
        with tmp_nix.open("w", encoding="utf-8") as f:
            f.write(nix_content)
        os.chmod(tmp_nix, 0o644)

        safe_log("[1/3] Validation syntaxique Nix (nix-instantiate --parse)...\n")
        rc = stream_cmd(["nix-instantiate", "--parse", str(tmp_nix)], check=False)
        if rc != 0:
            tmp_nix.unlink(missing_ok=True)
            raise RuntimeError("Erreur de syntaxe Nix détectée. Aucun changement appliqué.")

        os.replace(tmp_nix, MANAGED_NIX)
        save_state(new_state)

        for m in new_state.get("mounts", []):
            mp = Path(m["mountPoint"])
            mp.mkdir(parents=True, exist_ok=True)

        REBUILD_SENTINEL.write_text(str(os.getpid()), encoding="utf-8")
        try:
            action = "dry-build" if mode == "dry-build" else "switch"
            safe_log("[2/3] Exécution de nixos-rebuild " + action + "...\n")
            stream_cmd(["nixos-rebuild", action], check=True)
            safe_log("[3/3] Configuration NixOS appliquée avec succès.\n")
        except Exception:
            safe_log("[ROLLBACK] Échec de nixos-rebuild : restauration de l'état sain précédent...\n")
            if had_old_nix and old_nix_backup.exists():
                shutil.copy2(old_nix_backup, MANAGED_NIX)
            else:
                MANAGED_NIX.write_text("# Fichier réinitialisé suite à rollback\n{ config, pkgs, lib, ... }:\n{\n}\n", encoding="utf-8")
            if had_old_state and old_state_backup.exists():
                shutil.copy2(old_state_backup, STATE_FILE)
            else:
                STATE_FILE.unlink(missing_ok=True)
            raise
        finally:
            REBUILD_SENTINEL.unlink(missing_ok=True)
            # Nettoyage des anciens backups (> 20)
            backups = sorted(BACKUP_DIR.glob("cockpit-managed-*.nix"))
            for old in backups[:-20]:
                old.unlink(missing_ok=True)
            s_backups = sorted(BACKUP_DIR.glob("state-*.json"))
            for old in s_backups[:-20]:
                old.unlink(missing_ok=True)

    with_lock(_run)


def list_generations() -> list:
    gens = []
    current_target = ""
    if SYSTEM_PROFILE.is_symlink():
        current_target = os.readlink(SYSTEM_PROFILE)
    booted_real = os.path.realpath("/run/booted-system") if Path("/run/booted-system").exists() else ""

    for link in sorted(glob.glob("/nix/var/nix/profiles/system-*-link")):
        p = Path(link)
        m = re.search(r"system-(\d+)-link$", p.name)
        if not m:
            continue
        gen_id = int(m.group(1))
        stat = p.lstat()
        dt = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        ver_file = p / "nixos-version"
        ver = ver_file.read_text(encoding="utf-8").strip() if ver_file.exists() else "NixOS"
        kernel = ""
        mod_dir = p / "kernel-modules" / "lib" / "modules"
        if mod_dir.exists():
            kdirs = [d.name for d in mod_dir.iterdir() if d.is_dir()]
            if kdirs:
                kernel = kdirs[0]
        gens.append({
            "id": gen_id,
            "date": dt,
            "nixos_version": ver,
            "kernel": kernel,
            "current": (p.name == Path(current_target).name),
            "booted": bool(booted_real and os.path.realpath(str(p)) == booted_real),
        })

    gens.sort(key=lambda x: x["id"], reverse=True)
    return gens


def check_reboot_required() -> dict:
    booted = Path("/run/booted-system")
    nxt = SYSTEM_PROFILE
    reboot_required = False
    booted_kernel = ""
    next_kernel = ""
    try:
        if booted.exists() and nxt.exists():
            for sub in ("kernel", "initrd", "kernel-modules"):
                bp = booted / sub
                np = nxt / sub
                if bp.exists() and np.exists() and os.path.realpath(str(bp)) != os.path.realpath(str(np)):
                    reboot_required = True
            bmod = booted / "kernel-modules" / "lib" / "modules"
            nmod = nxt / "kernel-modules" / "lib" / "modules"
            if bmod.exists():
                bdirs = [d.name for d in bmod.iterdir() if d.is_dir()]
                if bdirs:
                    booted_kernel = bdirs[0]
            if nmod.exists():
                ndirs = [d.name for d in nmod.iterdir() if d.is_dir()]
                if ndirs:
                    next_kernel = ndirs[0]
    except Exception:
        pass
    return {
        "reboot_required": reboot_required,
        "booted_kernel": booted_kernel,
        "next_kernel": next_kernel,
    }


def load_updates_cache() -> dict:
    if not UPDATES_CACHE.exists():
        return {"timestamp": 0, "updates": []}
    try:
        return json.loads(UPDATES_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {"timestamp": 0, "updates": []}


def save_updates_cache(updates: list) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPDATES_CACHE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"timestamp": int(time.time()), "updates": updates}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o644)
    os.replace(tmp, UPDATES_CACHE)


def cmd_updates_check() -> None:
    ensure_root()

    def _check():
        safe_log("[1/3] Mise à jour des canaux NixOS (nix-channel --update)...\n")
        stream_cmd(["nix-channel", "--update"], check=False)

        safe_log("[2/3] Évaluation de la prochaine génération (nixos-rebuild build)...\n")
        target_path = ""
        with tempfile.TemporaryDirectory(prefix="cockpit-nixos-check-") as tmpdir:
            stream_cmd(["nixos-rebuild", "build"], check=True, cwd=tmpdir)
            result_link = Path(tmpdir) / "result"
            if result_link.exists():
                target_path = os.path.realpath(str(result_link))

        current_sys = os.path.realpath("/run/current-system")
        updates = []
        if target_path and Path(target_path).exists() and os.path.realpath(target_path) != current_sys:
            safe_log("[3/3] Calcul des différences de paquets (nix store diff-closures)...\n")
            diff_res = subprocess.run(
                ["nix", "--extra-experimental-features", "nix-command", "store", "diff-closures", current_sys, target_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if diff_res.returncode == 0:
                for line in diff_res.stdout.splitlines():
                    m = re.match(r"^([a-zA-Z0-9._+-]+):\s+([^,\n]+)", line.strip())
                    if not m:
                        continue
                    name, change = m.group(1), m.group(2).strip()
                    if "->" not in change:
                        continue
                    parts = change.split("->", 1)
                    old_v = parts[0].strip().lstrip("∅ ")
                    new_v = parts[1].strip().split(",")[0].strip()
                    if not new_v or new_v == "∅":
                        continue
                    is_sec = any(k in name.lower() for k in ("linux", "openssl", "openssh", "sudo", "polkit", "systemd", "glibc"))
                    updates.append({
                        "id": name + ";" + new_v + ";x86_64;nixos",
                        "name": name,
                        "old_version": old_v,
                        "new_version": new_v,
                        "info": 8 if is_sec else 5,
                        "security": is_sec,
                        "summary": "Mise à jour NixOS : " + old_v + " -> " + new_v,
                    })
                    if len(updates) >= 120:
                        break
            if not updates:
                ver_file = Path(target_path) / "nixos-version"
                new_ver = ver_file.read_text(encoding="utf-8").strip() if ver_file.exists() else "latest"
                updates.append({
                    "id": "nixos-system;" + new_ver + ";x86_64;nixos",
                    "name": "nixos-system",
                    "old_version": "current",
                    "new_version": new_ver,
                    "info": 5,
                    "security": False,
                    "summary": "Nouvelle génération système NixOS (" + new_ver + ")",
                })

        save_updates_cache(updates)
        if updates:
            safe_log("Vérification terminée : " + str(len(updates)) + " mise(s) à jour disponible(s).\n")
        else:
            safe_log("Vérification terminée : le système NixOS est à jour.\n")

    with_lock(_check)


def cmd_updates_apply() -> None:
    ensure_root()

    def _apply():
        REBUILD_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        REBUILD_SENTINEL.write_text(str(os.getpid()), encoding="utf-8")
        try:
            safe_log("[1/2] Application des mises à jour NixOS (nixos-rebuild switch --upgrade)...\n")
            stream_cmd(["nixos-rebuild", "switch", "--upgrade"], check=True)
            save_updates_cache([])
            safe_log("[2/2] Mises à jour appliquées sur une nouvelle génération NixOS.\n")
        finally:
            REBUILD_SENTINEL.unlink(missing_ok=True)

    with_lock(_apply)


def read_hwmon_telemetry() -> dict:
    sensors = []
    fan_rpm = None
    apu_temp_c = None
    steamdeck_detected = False

    for hw_dir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        hp = Path(hw_dir)
        name_file = hp / "name"
        if not name_file.exists():
            continue
        chip = name_file.read_text(encoding="utf-8", errors="ignore").strip()
        if "steamdeck" in chip.lower() or "jupiter" in chip.lower():
            steamdeck_detected = True

        for fan_file in sorted(hp.glob("fan*_input")):
            try:
                rpm = int(fan_file.read_text(encoding="utf-8").strip())
                lbl_file = hp / fan_file.name.replace("_input", "_label")
                lbl = lbl_file.read_text(encoding="utf-8").strip() if lbl_file.exists() else fan_file.name
                sensors.append({"chip": chip, "label": lbl, "value": str(rpm) + " RPM"})
                if fan_rpm is None or "steamdeck" in chip.lower():
                    fan_rpm = rpm
            except Exception:
                pass

        for temp_file in sorted(hp.glob("temp*_input")):
            try:
                milli = int(temp_file.read_text(encoding="utf-8").strip())
                celsius = milli / 1000.0
                lbl_file = hp / temp_file.name.replace("_input", "_label")
                lbl = lbl_file.read_text(encoding="utf-8").strip() if lbl_file.exists() else temp_file.name
                sensors.append({"chip": chip, "label": lbl, "value": "{:.1f} °C".format(celsius)})
                if chip in ("k10temp", "amdgpu", "coretemp", "zenpower") and apu_temp_c is None:
                    apu_temp_c = celsius
            except Exception:
                pass

    return {
        "steamdeck_detected": steamdeck_detected,
        "fan_rpm": fan_rpm,
        "apu_temp_c": apu_temp_c,
        "sensors": sensors[:16],
    }


def validate_package(pkg: str) -> str:
    pkg = pkg.strip()
    if not PKG_RE.match(pkg):
        raise ValueError("Nom de paquet nixpkgs invalide : " + pkg)
    return pkg


def parse_ports(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        items = [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]
    out = []
    for item in items:
        port = int(item)
        if port < 1 or port > 65535:
            raise ValueError("Port hors plage (1-65535) : " + str(port))
        out.append(port)
    return sorted(set(out))


def parse_interfaces(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        items = [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]
    out = []
    for iface in items:
        if not IFACE_RE.match(iface):
            raise ValueError("Nom d'interface invalide : " + iface)
        out.append(iface)
    return sorted(set(out))


def parse_nameservers(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        items = [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]
    out = []
    for ns in items:
        ipaddress.ip_address(ns)
        out.append(ns)
    return out


def validate_mount(payload: dict) -> dict:
    mp = os.path.normpath(str(payload.get("mountPoint", "")).strip())
    dev = str(payload.get("device", "")).strip()
    fs_type = str(payload.get("fsType", "")).strip()
    raw_opts = payload.get("options", "defaults,nofail")

    if mp in FORBIDDEN_MOUNTS or not mp.startswith(ALLOWED_MOUNT_PREFIXES):
        raise ValueError(
            "Point de montage refusé par sécurité. Préfixes autorisés : " + ", ".join(ALLOWED_MOUNT_PREFIXES)
        )
    if not DEV_RE.match(dev):
        raise ValueError("Source/device invalide : " + dev)
    if fs_type not in ALLOWED_FSTYPES:
        raise ValueError("Type de système de fichiers non autorisé : " + fs_type)

    if isinstance(raw_opts, list):
        opts = [str(o).strip() for o in raw_opts if str(o).strip()]
    else:
        opts = [o.strip() for o in str(raw_opts).split(",") if o.strip()]
    for o in opts:
        if not OPT_RE.match(o):
            raise ValueError("Option de montage invalide : " + o)
    if fs_type == "none" and "bind" not in opts:
        opts.append("bind")

    return {"mountPoint": mp, "device": dev, "fsType": fs_type, "options": opts}


def cmd_pkg_search(query: str) -> None:
    q = query.strip()
    if len(q) < 2 or not re.match(r"^[a-zA-Z0-9._+-]+$", q):
        raise ValueError("Requête de recherche invalide.")
    res = subprocess.run(
        ["nix-env", "-qaP", "--description", "--json", ".*" + q + ".*"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if res.returncode != 0:
        print(json.dumps([]))
        return
    parsed = json.loads(res.stdout or "{}")
    out = []
    for attr, meta in parsed.items():
        clean_attr = re.sub(r"^nixos\.", "", attr)
        out.append({
            "attr": clean_attr,
            "pname": meta.get("pname", clean_attr),
            "description": (meta.get("meta") or {}).get("description", ""),
        })
        if len(out) >= 25:
            break
    print(json.dumps(out))


def split_nmcli_terse(line: str) -> list:
    return [part.replace("\\:", ":") for part in re.split(r"(?<!\\):", line)]


def sync_nm_profiles_from_cli() -> dict:
    """Extrait les profils NetworkManager filaires/bridges/VLAN via nmcli.
    Note sécurité : les profils Wi-Fi (802-11-wireless) sont volontairement exclus
    pour ne jamais écrire de clés PSK en clair dans /etc/nixos/cockpit-managed.nix."""
    res = subprocess.run(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"],
        capture_output=True,
        text=True,
        check=True,
    )
    ignore_prefixes = ("lo", "podman", "cni-", "virbr", "vnet", "veth", "docker", "br-", "tun", "tap")
    profiles = {}
    for line in res.stdout.splitlines():
        parts = split_nmcli_terse(line)
        if len(parts) < 4:
            continue
        name, uuid, ctype, dev = parts[0], parts[1], parts[2], parts[3]
        if ctype not in ("802-3-ethernet", "ethernet", "bridge", "vlan", "bond"):
            continue
        if any(dev.startswith(p) for p in ignore_prefixes) or any(name.startswith(p) for p in ignore_prefixes):
            continue
        if not NM_KEY_RE.match(name):
            continue

        detail = subprocess.run(
            ["nmcli", "-t", "-f", "connection.interface-name,connection.autoconnect,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,ipv6.method", "connection", "show", uuid],
            capture_output=True,
            text=True,
            check=True,
        )
        kv = {}
        for dline in detail.stdout.splitlines():
            if ":" in dline:
                k, v = dline.split(":", 1)
                kv[k.strip()] = v.strip()

        nm_type = "ethernet" if ctype == "802-3-ethernet" else ctype
        prof = {
            "connection": {
                "id": name,
                "type": nm_type,
                "autoconnect": kv.get("connection.autoconnect", "yes") in ("yes", "true", "1"),
            },
            "ipv4": {
                "method": kv.get("ipv4.method", "auto") or "auto",
            },
            "ipv6": {
                "method": kv.get("ipv6.method", "auto") or "auto",
            },
        }
        ifname = kv.get("connection.interface-name") or dev
        if ifname and IFACE_RE.match(ifname) and ifname != "--":
            prof["connection"]["interface-name"] = ifname

        addr = kv.get("ipv4.addresses", "").strip()
        gw = kv.get("ipv4.gateway", "").strip()
        if addr and addr != "--":
            first_addr = addr.split(",")[0].strip()
            ipaddress.ip_interface(first_addr)
            if gw and gw != "--":
                ipaddress.ip_address(gw)
                prof["ipv4"]["address1"] = first_addr + "," + gw
            else:
                prof["ipv4"]["address1"] = first_addr

        dns = kv.get("ipv4.dns", "").strip()
        if dns and dns != "--":
            dns_list = [d.strip() for d in dns.replace(",", ";").split(";") if d.strip()]
            for d in dns_list:
                ipaddress.ip_address(d)
            if dns_list:
                prof["ipv4"]["dns"] = ";".join(dns_list) + ";"

        profiles[name] = prof
    return profiles


def _query_timer(unit: str) -> dict:
    info = {"active": False, "next": "", "calendar": ""}
    try:
        res = subprocess.run(
            ["systemctl", "show", unit, "--property=LoadState,ActiveState,NextElapseUSecRealtime,TimersCalendar"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        kv = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        if kv.get("LoadState") == "loaded" and kv.get("ActiveState") in ("active", "waiting"):
            info["active"] = True
        nxt = kv.get("NextElapseUSecRealtime", "")
        if nxt and nxt not in ("n/a", "0"):
            info["next"] = nxt
        tc = kv.get("TimersCalendar", "")
        m = re.search(r"OnCalendar=([^;\}]+)", tc)
        if m:
            info["calendar"] = m.group(1).strip()
    except Exception:
        pass
    return info


def _detect_auto_optimise_store() -> bool:
    nix_conf = Path("/etc/nix/nix.conf")
    if not nix_conf.exists():
        return False
    try:
        for line in nix_conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == "auto-optimise-store":
                return v.strip().lower() == "true"
    except OSError:
        pass
    return False


def read_automation_status(state: dict) -> dict:
    upg_timer = _query_timer("nixos-upgrade.timer")
    opt_timer = _query_timer("nix-optimise.timer")
    gc_timer = _query_timer("nix-gc.timer")
    live_auto_opt = _detect_auto_optimise_store()

    saved = state.get("automation") if isinstance(state.get("automation"), dict) else {}
    saved_au = saved.get("autoUpgrade") if isinstance(saved.get("autoUpgrade"), dict) else {}
    saved_so = saved.get("storeOptimise") if isinstance(saved.get("storeOptimise"), dict) else {}
    saved_gc = saved.get("gc") if isinstance(saved.get("gc"), dict) else {}

    return {
        "autoUpgrade": {
            "enable": bool(saved_au["enable"]) if "enable" in saved_au else upg_timer["active"],
            "dates": saved_au.get("dates") or upg_timer["calendar"] or "03:30",
            "allowReboot": bool(saved_au["allowReboot"]) if "allowReboot" in saved_au else True,
            "useRebootWindow": bool(saved_au["useRebootWindow"]) if "useRebootWindow" in saved_au else True,
            "rebootLower": saved_au.get("rebootLower") or "03:00",
            "rebootUpper": saved_au.get("rebootUpper") or "05:30",
            "liveActive": upg_timer["active"],
            "nextElapse": upg_timer["next"],
        },
        "storeOptimise": {
            "autoOptimiseStore": bool(saved_so["autoOptimiseStore"]) if "autoOptimiseStore" in saved_so else live_auto_opt,
            "liveAutoOptimiseStore": live_auto_opt,
            "periodicEnable": bool(saved_so["periodicEnable"]) if "periodicEnable" in saved_so else opt_timer["active"],
            "periodicDates": saved_so.get("periodicDates") or opt_timer["calendar"] or "03:45",
            "livePeriodicActive": opt_timer["active"],
            "nextElapse": opt_timer["next"],
        },
        "gc": {
            "automatic": bool(saved_gc["automatic"]) if "automatic" in saved_gc else gc_timer["active"],
            "dates": saved_gc.get("dates") or gc_timer["calendar"] or "weekly",
            "retention": saved_gc.get("retention") or "14d",
            "liveActive": gc_timer["active"],
            "nextElapse": gc_timer["next"],
        },
    }


def validate_calendar_expr(expr: str, field_name: str) -> str:
    val = str(expr or "").strip()
    if not val or not CALENDAR_RE.match(val):
        raise ValueError("Expression OnCalendar invalide pour " + field_name + " : " + val)
    if shutil.which("systemd-analyze"):
        res = subprocess.run(
            ["systemd-analyze", "calendar", val],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            raise ValueError("Expression systemd OnCalendar invalide (" + val + ") : " + (res.stderr or res.stdout).strip())
    return val


def validate_automation(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Payload automation invalide.")
    au_in = payload.get("autoUpgrade") or {}
    so_in = payload.get("storeOptimise") or {}

    au_enable = bool(au_in.get("enable", False))
    au_dates = validate_calendar_expr(au_in.get("dates", "03:30"), "autoUpgrade.dates")
    au_reboot = bool(au_in.get("allowReboot", False))
    use_win = bool(au_in.get("useRebootWindow", True))
    win_lower = str(au_in.get("rebootLower", "03:00")).strip()
    win_upper = str(au_in.get("rebootUpper", "05:30")).strip()
    if au_reboot and use_win:
        if not HHMM_RE.match(win_lower) or not HHMM_RE.match(win_upper):
            raise ValueError("Fenêtre de redémarrage invalide (format attendu HH:MM).")

    so_auto = bool(so_in.get("autoOptimiseStore", False))
    so_periodic = bool(so_in.get("periodicEnable", False))
    so_dates = validate_calendar_expr(so_in.get("periodicDates", "03:45"), "nix.optimise.dates")

    gc_in = payload.get("gc") or {}
    gc_auto = bool(gc_in.get("automatic", False))
    gc_dates = validate_calendar_expr(gc_in.get("dates", "weekly"), "nix.gc.dates")
    gc_retention = str(gc_in.get("retention", "14d")).strip()
    if gc_retention not in ("7d", "14d", "30d", "60d", "90d", "all"):
        raise ValueError("Rétention GC invalide : " + gc_retention)

    return {
        "autoUpgrade": {
            "enable": au_enable,
            "dates": au_dates,
            "allowReboot": au_reboot,
            "useRebootWindow": use_win,
            "rebootLower": win_lower,
            "rebootUpper": win_upper,
        },
        "storeOptimise": {
            "autoOptimiseStore": so_auto,
            "periodicEnable": so_periodic,
            "periodicDates": so_dates,
        },
        "gc": {
            "automatic": gc_auto,
            "dates": gc_dates,
            "retention": gc_retention,
        },
    }


def main() -> int:
    # Survit aux déconnexions/reloads temporaires de Cockpit ou dbus-broker pendant nixos-rebuild
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    ensure_nix_env()
    if len(sys.argv) < 2:
        print("Usage: cockpit-nixos-ctl <command> [args...]", file=sys.stderr)
        return 2

    cmd = sys.argv[1]
    try:
        if cmd == "status":
            state = load_state()
            managed = MANAGED_NIX.read_text(encoding="utf-8") if MANAGED_NIX.exists() else render_managed_nix(state)
            print(json.dumps({
                "state": state,
                "generations": list_generations(),
                "managed_nix": managed,
                "telemetry": read_hwmon_telemetry(),
                "updates": load_updates_cache(),
                "system_info": check_reboot_required(),
                "automation": read_automation_status(state),
            }))
            return 0

        if cmd == "telemetry":
            print(json.dumps(read_hwmon_telemetry()))
            return 0

        if cmd == "generations":
            print(json.dumps(list_generations()))
            return 0

        if cmd == "pkg-search" and len(sys.argv) >= 3:
            cmd_pkg_search(sys.argv[2])
            return 0

        if cmd == "rebuild":
            apply_state_transactional(load_state(), mode="switch")
            return 0

        if cmd == "dry-build":
            apply_state_transactional(load_state(), mode="dry-build")
            return 0

        if cmd == "pkg-add" and len(sys.argv) >= 3:
            pkg = validate_package(sys.argv[2])
            state = load_state()
            if pkg not in state["packages"]:
                state["packages"].append(pkg)
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "pkg-remove" and len(sys.argv) >= 3:
            pkg = validate_package(sys.argv[2])
            state = load_state()
            state["packages"] = [p for p in state["packages"] if p != pkg]
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "fw-set" and len(sys.argv) >= 3:
            payload = json.loads(sys.argv[2])
            state = load_state()
            state["firewall"] = {
                "enable": bool(payload.get("enable", True)),
                "allowedTCPPorts": parse_ports(payload.get("tcp", "")),
                "allowedUDPPorts": parse_ports(payload.get("udp", "")),
                "trustedInterfaces": parse_interfaces(payload.get("trusted", "")),
            }
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd in ("fw-port-add", "fw-port-remove") and len(sys.argv) >= 4:
            port = parse_ports([sys.argv[2]])[0]
            proto = sys.argv[3].lower()
            if proto not in ("tcp", "udp"):
                raise ValueError("Protocole invalide : " + proto)
            state = load_state()
            key = "allowedTCPPorts" if proto == "tcp" else "allowedUDPPorts"
            ports = set(int(p) for p in state["firewall"].get(key, []))
            if cmd == "fw-port-add":
                ports.add(port)
            else:
                ports.discard(port)
            state["firewall"][key] = sorted(ports)
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "fw-ports-batch" and len(sys.argv) >= 4:
            action = sys.argv[2].lower()
            if action not in ("add", "remove"):
                raise ValueError("Action batch invalide : " + action)
            entries = json.loads(sys.argv[3])
            state = load_state()
            tcp = set(int(p) for p in state["firewall"].get("allowedTCPPorts", []))
            udp = set(int(p) for p in state["firewall"].get("allowedUDPPorts", []))
            for item in entries:
                port = parse_ports([item["port"]])[0]
                proto = str(item["proto"]).lower()
                if proto not in ("tcp", "udp"):
                    raise ValueError("Protocole invalide : " + proto)
                target = tcp if proto == "tcp" else udp
                if action == "add":
                    target.add(port)
                else:
                    target.discard(port)
            state["firewall"]["allowedTCPPorts"] = sorted(tcp)
            state["firewall"]["allowedUDPPorts"] = sorted(udp)
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "net-set" and len(sys.argv) >= 3:
            payload = json.loads(sys.argv[2])
            state = load_state()
            hostname = str(payload.get("hostname", "")).strip()
            if hostname and not HOST_RE.match(hostname):
                raise ValueError("Nom d'hôte invalide : " + hostname)
            state["network"]["hostname"] = hostname
            state["network"]["nameservers"] = parse_nameservers(payload.get("nameservers", ""))
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "sync-networkmanager":
            ensure_root()
            state = load_state()
            state["network"]["nmProfiles"] = sync_nm_profiles_from_cli()
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "mount-add" and len(sys.argv) >= 3:
            payload = json.loads(sys.argv[2])
            entry = validate_mount(payload)
            state = load_state()
            state["mounts"] = [m for m in state["mounts"] if m["mountPoint"] != entry["mountPoint"]]
            state["mounts"].append(entry)
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "mount-remove" and len(sys.argv) >= 3:
            mp = os.path.normpath(sys.argv[2].strip())
            state = load_state()
            state["mounts"] = [m for m in state["mounts"] if m["mountPoint"] != mp]
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "mounts-sync" and len(sys.argv) >= 3:
            ensure_root()
            raw_list = json.loads(sys.argv[2])
            if not isinstance(raw_list, list):
                raise ValueError("mounts-sync attend une liste JSON.")
            sync_prefixes = ("/mnt/", "/srv/", "/media/", "/data/")
            synced = [validate_mount(item) for item in raw_list]
            synced = [m for m in synced if m["mountPoint"].startswith(sync_prefixes)]
            state = load_state()
            preserved = [m for m in state.get("mounts", []) if not m.get("mountPoint", "").startswith(sync_prefixes)]
            merged = sorted(preserved + synced, key=lambda m: m["mountPoint"])
            current_sorted = sorted(state.get("mounts", []), key=lambda m: m.get("mountPoint", ""))
            if json.dumps(merged, sort_keys=True) == json.dumps(current_sorted, sort_keys=True):
                return 0
            state["mounts"] = merged
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "switch-generation" and len(sys.argv) >= 3:
            ensure_root()
            gen_id = int(sys.argv[2])
            gen_path = Path("/nix/var/nix/profiles/system-" + str(gen_id) + "-link")
            switch_bin = gen_path / "bin" / "switch-to-configuration"
            if not switch_bin.exists():
                raise FileNotFoundError("Génération #" + str(gen_id) + " introuvable.")
            with_lock(lambda: (
                stream_cmd(["nix-env", "-p", str(SYSTEM_PROFILE), "--switch-generation", str(gen_id)]),
                stream_cmd([str(switch_bin), "switch"]),
            ))
            return 0

        if cmd == "rollback":
            ensure_root()
            with_lock(lambda: stream_cmd(["nixos-rebuild", "switch", "--rollback"]))
            return 0

        if cmd == "gc":
            ensure_root()
            def _gc():
                stream_cmd(["nix-collect-garbage", "-d"])
                stream_cmd(["/run/current-system/bin/switch-to-configuration", "boot"])
            with_lock(_gc)
            return 0

        if cmd == "optimise-store":
            ensure_root()
            with_lock(lambda: stream_cmd(["nix-store", "--optimise"]))
            return 0

        if cmd == "updates-check":
            cmd_updates_check()
            return 0

        if cmd == "updates-apply":
            cmd_updates_apply()
            return 0

        if cmd == "automation-set" and len(sys.argv) >= 3:
            payload = json.loads(sys.argv[2])
            state = load_state()
            state["automation"] = validate_automation(payload)
            apply_state_transactional(state, mode="switch")
            return 0

        if cmd == "reboot":
            ensure_root()
            stream_cmd(["systemctl", "reboot"])
            return 0

        print("Commande inconnue : " + cmd, file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERREUR : " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
