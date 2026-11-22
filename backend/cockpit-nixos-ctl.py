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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/var/lib/cockpit-nixos")
STATE_FILE = STATE_DIR / "state.json"
BACKUP_DIR = STATE_DIR / "backups"
MANAGED_NIX = Path("/etc/nixos/cockpit-managed.nix")
LOCK_FILE = Path("/run/lock/cockpit-nixos.lock")
REBUILD_SENTINEL = Path("/run/cockpit-nixos/rebuilding")
SYSTEM_PROFILE = Path("/nix/var/nix/profiles/system")

PKG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]*(?:\.[a-zA-Z0-9][a-zA-Z0-9._+-]*)*$")
HOST_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
IFACE_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,15}$")
OPT_RE = re.compile(r"^[a-zA-Z0-9_=,./:@-]+$")
DEV_RE = re.compile(r"^[a-zA-Z0-9_./:@=+-]+$")
NM_KEY_RE = re.compile(r"^[a-zA-Z0-9._ -]{1,64}$")

ALLOWED_FSTYPES = {"ext4", "xfs", "btrfs", "vfat", "ntfs", "nfs", "nfs4", "cifs", "tmpfs", "none", "auto"}
ALLOWED_MOUNT_PREFIXES = ("/mnt/", "/srv/", "/media/", "/data/", "/var/lib/libvirt/images", "/var/lib/containers")
FORBIDDEN_MOUNTS = {"/", "/boot", "/etc", "/nix", "/nix/store", "/usr", "/var", "/proc", "/sys", "/dev", "/run"}

DEFAULT_STATE = {
    "packages": [],
    "firewall": {
        "enable": True,
        "allowedTCPPorts": [],
        "allowedUDPPorts": [],
        "trustedInterfaces": ["podman0", "virbr0"],
    },
    "network": {
        "hostname": "",
        "nameservers": [],
        "nmProfiles": {},
    },
    "mounts": [],
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
    lines.append("  networking.firewall = {")
    if fw.get("enable") is False:
        lines.append("    enable = lib.mkForce false;")
    tcp_ports = sorted(set(int(p) for p in fw.get("allowedTCPPorts", [])))
    udp_ports = sorted(set(int(p) for p in fw.get("allowedUDPPorts", [])))
    trusted = sorted(set(fw.get("trustedInterfaces", [])))
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

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def stream_cmd(cmd: list, check: bool = True) -> int:
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
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

        print("[1/3] Validation syntaxique Nix (nix-instantiate --parse)...", flush=True)
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
            print("[2/3] Exécution de nixos-rebuild " + action + "...", flush=True)
            stream_cmd(["nixos-rebuild", action], check=True)
            print("[3/3] Configuration NixOS appliquée avec succès.", flush=True)
        except Exception:
            print("[ROLLBACK] Échec de nixos-rebuild : restauration de l'état sain précédent...", flush=True)
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
        })

    gens.sort(key=lambda x: x["id"], reverse=True)
    return gens


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


def read_health_status() -> dict:
    hc_file = Path("/var/lib/nixos-health-checker/state.json")
    data = {
        "status": "NOT_CONFIGURED",
        "last_good_gen": None,
        "unhealthy_boot_attempts": 0,
        "max_boot_attempts": 3,
        "last_check_time": "",
        "probes": [],
        "rollback_history": [],
        "auto_upgrade_next": "",
        "auto_upgrade_active": False,
    }
    if hc_file.exists():
        try:
            data.update(json.loads(hc_file.read_text(encoding="utf-8")))
        except Exception:
            pass
    try:
        res = subprocess.run(
            ["systemctl", "show", "nixos-upgrade.timer", "--property=ActiveState,NextElapseUSecRealtime"],
            capture_output=True,
            text=True,
        )
        for line in res.stdout.splitlines():
            if line.startswith("ActiveState="):
                data["auto_upgrade_active"] = (line.split("=", 1)[1].strip() == "active")
            elif line.startswith("NextElapseUSecRealtime="):
                data["auto_upgrade_next"] = line.split("=", 1)[1].strip()
    except Exception:
        pass
    return data


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


def main() -> int:
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
                "health": read_health_status(),
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

        if cmd == "health-check":
            ensure_root()
            stream_cmd(["/run/current-system/sw/bin/nixos-health-checker", "--check-only", "--update-probes"], check=False)
            return 0

        if cmd == "trigger-upgrade":
            ensure_root()
            with_lock(lambda: stream_cmd(["systemctl", "start", "--wait", "nixos-upgrade.service"]))
            return 0

        print("Commande inconnue : " + cmd, file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERREUR : " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
