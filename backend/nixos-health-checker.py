#!/usr/bin/env python3
"""
nixos-health-checker : Équivalent NixOS du health-checker d'openSUSE MicroOS.

Fonctionnement en deux phases (comme MicroOS + systemd Automatic Boot Assessment) :
  1. Phase précoce (--boot-counter, lancée dès local-fs.target) :
     - Identifie la génération NixOS démarrée (/run/current-system -> system-N-link).
     - Si la génération courante diffère de la dernière génération saine vérifiée
       (last_good_gen), incrémente de façon atomique + fsync le compteur
       `unhealthy_boot_attempts`.
     - Si le compteur atteint `max_boot_attempts` (défaut: 3) suite à des crashs,
       kernel panics, watchdogs matériels ou échecs répétés : déclenche
       IMMÉDIATEMENT le rollback atomique vers `last_good_gen` et redémarre.

  2. Phase complète (--verify, avant boot-complete.target) :
     - Exécute les sondes critiques (système de fichiers, services systemd,
       réseau IP, port Cockpit, socket Podman, thermique Steam Deck).
     - En cas de succès : remet le compteur à 0, enregistre la génération courante
       comme `last_good_gen` et quitte avec code 0 (permettant à
       systemd-bless-boot de bénir définitivement l'entrée de boot).
     - En cas d'échec sur une NOUVELLE génération (!= last_good_gen) :
       - Tentatives < max_boot_attempts : quitte en erreur (non-bénie) et
         redémarre après délai pour retenter.
       - Tentatives >= max_boot_attempts : rollback automatique vers
         `last_good_gen` + mise à jour du bootloader + redémarrage.
     - En cas d'échec sur la génération DÉJÀ connue comme saine (ex: box LAN
       éteinte) : N'effectue PAS de rollback en boucle ; passe en état DEGRADED.

  3. Phase post-upgrade à chaud (--live-check) :
     - Lancée après un nixos-upgrade sans reboot : vérifie les sondes et
       effectue un `nixos-rebuild switch --rollback` à chaud si une sonde échoue.
"""

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/var/lib/nixos-health-checker")
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = Path("/var/log/nixos-health-checker.log")
SYSTEM_PROFILE = Path("/nix/var/nix/profiles/system")


def log(msg: str) -> None:
    line = "[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + msg
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def ensure_nix_env() -> None:
    path = os.environ.get("PATH", "")
    sw_bin = "/run/current-system/sw/bin"
    if sw_bin not in path.split(":"):
        os.environ["PATH"] = sw_bin + ":/run/wrappers/bin:" + path
    if not os.environ.get("NIX_PATH"):
        root_channels = "/nix/var/nix/profiles/per-user/root/channels"
        if Path(root_channels + "/nixos").exists():
            os.environ["NIX_PATH"] = (
                "nixpkgs=" + root_channels + "/nixos"
                ":nixos-config=/etc/nixos/configuration.nix"
                ":" + root_channels
            )


def load_state() -> dict:
    default = {
        "last_good_gen": None,
        "last_good_system": "",
        "current_gen": None,
        "tracked_unhealthy_gen": None,
        "unhealthy_boot_attempts": 0,
        "max_boot_attempts": 3,
        "status": "UNKNOWN",
        "last_check_time": "",
        "probes": [],
        "failed_generations": [],
        "rollback_history": [],
    }
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        default.update(data)
        return default
    except Exception:
        return default


def save_state_durable(state: dict) -> None:
    """Écriture atomique + fsync fichier et répertoire pour résister aux panics/coupures."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)
    try:
        dir_fd = os.open(str(STATE_DIR), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        pass


def detect_generation(prefer_current: bool = False) -> tuple:
    """Retourne (gen_id: int | None, store_path: str) de la génération active ou démarrée."""
    first = Path("/run/current-system") if prefer_current else Path("/run/booted-system")
    second = Path("/run/booted-system") if prefer_current else Path("/run/current-system")
    active_target = ""
    if first.exists():
        active_target = os.path.realpath(str(first))
    elif second.exists():
        active_target = os.path.realpath(str(second))

    matched_id = None
    best_id = None
    for link in sorted(glob.glob("/nix/var/nix/profiles/system-*-link")):
        p = Path(link)
        m = re.search(r"system-(\d+)-link$", p.name)
        if not m:
            continue
        gid = int(m.group(1))
        if active_target and os.path.realpath(str(p)) == active_target:
            matched_id = gid
        if SYSTEM_PROFILE.is_symlink() and Path(os.readlink(SYSTEM_PROFILE)).name == p.name:
            best_id = gid

    return (matched_id if matched_id is not None else best_id, active_target)


def find_rollback_target(state: dict, current_gen: int) -> int:
    last_good = state.get("last_good_gen")
    if last_good is not None and int(last_good) != current_gen:
        cand = Path("/nix/var/nix/profiles/system-" + str(last_good) + "-link/bin/switch-to-configuration")
        if cand.exists():
            return int(last_good)

    failed = set(int(g) for g in state.get("failed_generations", []))
    gens = []
    for link in glob.glob("/nix/var/nix/profiles/system-*-link"):
        m = re.search(r"system-(\d+)-link$", Path(link).name)
        if m:
            gid = int(m.group(1))
            if gid != current_gen and gid not in failed:
                gens.append(gid)
    gens.sort(reverse=True)
    return gens[0] if gens else -1


def perform_boot_rollback(state: dict, current_gen: int, reason: str) -> None:
    ensure_nix_env()
    target = find_rollback_target(state, current_gen)
    if target < 0:
        log("CRITIQUE : Aucune génération antérieure valide trouvée pour rollback !")
        state["status"] = "ROLLBACK_NO_TARGET"
        save_state_durable(state)
        return

    log("ROLLBACK AUTOMATIQUE déclenché : génération #" + str(current_gen) + " -> #" + str(target) + " (" + reason + ")")
    failed = list(dict.fromkeys(state.get("failed_generations", []) + [current_gen]))
    state["failed_generations"] = failed[-20:]
    state["unhealthy_boot_attempts"] = 0
    state["tracked_unhealthy_gen"] = None
    state["status"] = "ROLLED_BACK"
    history = state.get("rollback_history", [])
    history.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from_gen": current_gen,
        "to_gen": target,
        "reason": reason,
    })
    state["rollback_history"] = history[-15:]
    save_state_durable(state)

    switch_bin = "/nix/var/nix/profiles/system-" + str(target) + "-link/bin/switch-to-configuration"
    subprocess.run(["nix-env", "-p", str(SYSTEM_PROFILE), "--switch-generation", str(target)], check=False)
    subprocess.run([switch_bin, "boot"], check=False)

    log("Bootloader mis à jour sur génération #" + str(target) + ". Redémarrage immédiat...")
    subprocess.run(["systemctl", "reboot"], check=False)


# -----------------------------------------------------------------------------
# SONDES DE SANTÉ (style openSUSE MicroOS health-checker plugins)
# -----------------------------------------------------------------------------
def probe_filesystems() -> dict:
    try:
        test_file = STATE_DIR / ".rw-probe"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        nix_ok = Path("/nix/store").is_dir() and Path("/etc/nixos").is_dir()
        return {"name": "fs_writable", "ok": nix_ok, "detail": "Racine /var/lib et /nix/store opérationnels" if nix_ok else "/nix/store ou /etc/nixos manquant"}
    except Exception as exc:
        return {"name": "fs_writable", "ok": False, "detail": "Système de fichiers en lecture seule ou erreur : " + str(exc)}


def is_unit_enabled(unit: str) -> bool:
    res = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True)
    return res.returncode == 0 or res.stdout.strip() in ("enabled", "static", "linked", "enabled-runtime")


def probe_critical_services(extra_units: list) -> dict:
    base_units = ["dbus.service", "systemd-journald.service", "cockpit.socket"]
    optional_if_enabled = ["NetworkManager.service", "sshd.service", "podman.socket", "libvirtd.service"]
    to_check = list(base_units) + list(extra_units)
    for u in optional_if_enabled:
        if u not in to_check and is_unit_enabled(u):
            to_check.append(u)

    bad = []
    for unit in to_check:
        res = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        st = res.stdout.strip()
        if st not in ("active", "listening"):
            bad.append(unit + "=" + (st or "inactive"))

    if bad:
        return {"name": "systemd_critical", "ok": False, "detail": "Unités non actives : " + ", ".join(bad)}
    return {"name": "systemd_critical", "ok": True, "detail": str(len(to_check)) + " unités critiques actives"}


def probe_network_ip() -> dict:
    res = subprocess.run(["ip", "-j", "addr", "show", "up"], capture_output=True, text=True)
    if res.returncode != 0:
        return {"name": "network_ip", "ok": False, "detail": "Impossible d'interroger ip addr"}
    try:
        ifaces = json.loads(res.stdout or "[]")
        addrs = []
        for iface in ifaces:
            name = iface.get("ifname", "")
            if name == "lo" or name.startswith(("podman", "cni-", "virbr", "veth", "docker")):
                continue
            for info in iface.get("addr_info", []):
                if info.get("scope") in ("global", "site"):
                    addrs.append(name + ":" + info.get("local", ""))
        if addrs:
            return {"name": "network_ip", "ok": True, "detail": ", ".join(addrs[:3])}
        return {"name": "network_ip", "ok": False, "detail": "Aucune adresse IP LAN/globale détectée"}
    except Exception as exc:
        return {"name": "network_ip", "ok": False, "detail": str(exc)}


def probe_cockpit_port(port: int) -> dict:
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                if s.connect_ex((host, port)) == 0:
                    return {"name": "cockpit_port", "ok": True, "detail": "Port TCP " + str(port) + " en écoute (" + host + ")"}
        except Exception:
            pass
    return {"name": "cockpit_port", "ok": False, "detail": "Cockpit ne répond pas sur le port " + str(port)}


def probe_thermal_safety() -> dict:
    max_temp = None
    chip_found = ""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        hp = Path(hw)
        nf = hp / "name"
        if not nf.exists():
            continue
        chip = nf.read_text(errors="ignore").strip()
        if chip not in ("k10temp", "amdgpu", "coretemp", "steamdeck_hwmon"):
            continue
        for tf in hp.glob("temp*_input"):
            try:
                t = int(tf.read_text().strip()) / 1000.0
                if max_temp is None or t > max_temp:
                    max_temp = t
                    chip_found = chip
            except Exception:
                pass
    if max_temp is None:
        return {"name": "thermal_safety", "ok": True, "detail": "Aucun capteur thermique critique"}
    ok = max_temp < 95.0
    return {"name": "thermal_safety", "ok": ok, "detail": "{:.1f} °C ({})".format(max_temp, chip_found)}


def run_all_probes(cockpit_port: int, extra_units: list, wait_seconds: int = 45) -> tuple:
    deadline = time.time() + max(0, wait_seconds)
    probes = []
    while True:
        probes = [
            probe_filesystems(),
            probe_critical_services(extra_units),
            probe_network_ip(),
            probe_cockpit_port(cockpit_port),
            probe_thermal_safety(),
        ]
        if all(p["ok"] for p in probes) or time.time() >= deadline:
            break
        time.sleep(5)
    return (all(p["ok"] for p in probes), probes)


def detect_cockpit_port(explicit_port: int) -> int:
    if explicit_port and explicit_port > 0:
        return explicit_port
    try:
        res = subprocess.run(["systemctl", "show", "cockpit.socket", "--property=Listen"], capture_output=True, text=True)
        m = re.search(r":(\d+)\b|\b(\d{2,5})\b", res.stdout)
        if m:
            p = int(m.group(1) or m.group(2))
            if 1 <= p <= 65535:
                return p
    except Exception:
        pass
    return 9090


def main() -> int:
    ensure_nix_env()
    parser = argparse.ArgumentParser(description="NixOS Health-Checker & Auto-Rollback (MicroOS-style)")
    parser.add_argument("--boot-counter", action="store_true", help="Phase 1 (précoce) : incrémente le compteur de boot et rollback si boot-loop")
    parser.add_argument("--verify", action="store_true", help="Phase 2 (boot-complete) : exécute les sondes, bénit ou rollback")
    parser.add_argument("--live-check", action="store_true", help="Phase post-upgrade à chaud : rollback à chaud si une sonde échoue")
    parser.add_argument("--check-only", action="store_true", help="Exécute les sondes en lecture seule et affiche le JSON")
    parser.add_argument("--update-probes", action="store_true", help="Avec --check-only : met à jour probes et last_check_time dans state.json")
    parser.add_argument("--max-attempts", type=int, default=3, help="Nombre max de démarrages échoués avant rollback")
    parser.add_argument("--cockpit-port", type=int, default=0, help="Port TCP de Cockpit à vérifier (0 = auto-détection)")
    parser.add_argument("--wait-seconds", type=int, default=45, help="Délai max d'attente stabilisation réseau/services")
    parser.add_argument("--extra-unit", action="append", default=[], help="Unité systemd supplémentaire à vérifier")
    args = parser.parse_args()

    cockpit_port = detect_cockpit_port(args.cockpit_port)
    state = load_state()
    state["max_boot_attempts"] = args.max_attempts
    current_gen, current_store = detect_generation(prefer_current=(args.live_check or args.check_only))
    state["current_gen"] = current_gen

    if args.check_only:
        ok, probes = run_all_probes(cockpit_port, args.extra_unit, wait_seconds=0)
        state["probes"] = probes
        state["last_check_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if args.update_probes and os.geteuid() == 0:
            save_state_durable(state)
        print(json.dumps({"healthy": ok, "state": state}, indent=2))
        return 0 if ok else 1

    # PHASE 1 : COMPTEUR PRÉCOCE AU BOOT (capture kernel panics, watchdogs et boot-loops)
    if args.boot_counter:
        last_good = state.get("last_good_gen")
        if current_gen is None:
            log("[boot-counter] Génération courante indéterminée, ignoré.")
            return 0
        if last_good is None:
            log("[boot-counter] Premier démarrage avec health-checker (génération #" + str(current_gen) + ").")
            save_state_durable(state)
            return 0
        if int(current_gen) == int(last_good):
            state["unhealthy_boot_attempts"] = 0
            state["tracked_unhealthy_gen"] = None
            save_state_durable(state)
            log("[boot-counter] Démarrage sur génération saine connue #" + str(current_gen) + ".")
            return 0

        if state.get("tracked_unhealthy_gen") != int(current_gen):
            state["tracked_unhealthy_gen"] = int(current_gen)
            state["unhealthy_boot_attempts"] = 0

        attempts = int(state.get("unhealthy_boot_attempts", 0)) + 1
        state["unhealthy_boot_attempts"] = attempts
        state["status"] = "BOOT_VERIFYING"
        save_state_durable(state)
        log("[boot-counter] Nouvelle génération #" + str(current_gen) + " (tentative " + str(attempts) + "/" + str(args.max_attempts) + ", last_good=#" + str(last_good) + ").")

        if attempts > args.max_attempts:
            perform_boot_rollback(
                state,
                int(current_gen),
                "Dépassement de " + str(args.max_attempts) + " démarrages sans validation (boot-loop / crash précoce)",
            )
            return 1
        return 0

    # PHASE 2 : VÉRIFICATION COMPLÈTE AVANT boot-complete.target
    if args.verify:
        ok, probes = run_all_probes(cockpit_port, args.extra_unit, wait_seconds=args.wait_seconds)
        state["probes"] = probes
        state["last_check_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if ok:
            state["last_good_gen"] = current_gen
            state["last_good_system"] = current_store
            state["unhealthy_boot_attempts"] = 0
            state["tracked_unhealthy_gen"] = None
            state["status"] = "HEALTHY"
            save_state_durable(state)
            log("[verify] SUCCÈS : Génération #" + str(current_gen) + " vérifiée et marquée SAINE (last_good).")
            return 0

        failed_names = [p["name"] + " (" + p["detail"] + ")" for p in probes if not p["ok"]]
        summary = "; ".join(failed_names)
        log("[verify] ÉCHEC sondes sur génération #" + str(current_gen) + " : " + summary)

        last_good = state.get("last_good_gen")
        if last_good is None or current_gen is None or int(current_gen) == int(last_good):
            # Garde-fou anti-boucle : on est déjà sur la génération connue comme saine
            state["status"] = "DEGRADED_ON_LAST_GOOD"
            save_state_durable(state)
            log("[verify] Déjà sur last_good (#" + str(current_gen) + ") : pas de rollback automatique pour éviter une boucle.")
            return 0

        attempts = max(1, int(state.get("unhealthy_boot_attempts", 1)))
        if attempts >= args.max_attempts:
            perform_boot_rollback(state, int(current_gen), "Sondes échouées (" + str(attempts) + "/" + str(args.max_attempts) + ") : " + summary)
            return 1

        state["status"] = "UNHEALTHY_RETRY_" + str(attempts)
        save_state_durable(state)
        log("[verify] Tentative " + str(attempts) + "/" + str(args.max_attempts) + " échouée sur #" + str(current_gen) + ". Redémarrage dans 15s pour nouvelle tentative...")
        time.sleep(15)
        subprocess.run(["systemctl", "reboot"], check=False)
        return 1

    # PHASE 3 : VÉRIFICATION POST-UPGRADE À CHAUD (sans reboot)
    if args.live_check:
        # Si un redémarrage est déjà en cours (ex. MAJ noyau via system.autoUpgrade), ne pas interférer :
        # la vérification se fera proprement au prochain boot par --boot-counter et --verify.
        sys_state = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True).stdout.strip()
        if sys_state == "stopping":
            log("[live-check] Redémarrage système déjà en cours : vérification reportée au prochain boot.")
            return 0
        for part in ("kernel", "initrd", "kernel-modules"):
            booted_p = Path("/run/booted-system") / part
            next_p = SYSTEM_PROFILE / part
            if booted_p.exists() and next_p.exists() and os.path.realpath(str(booted_p)) != os.path.realpath(str(next_p)):
                log("[live-check] Changement noyau/initrd détecté (" + part + ") : vérification reportée au prochain boot.")
                return 0

        ok, probes = run_all_probes(cockpit_port, args.extra_unit, wait_seconds=20)
        state["probes"] = probes
        state["last_check_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            state["last_good_gen"] = current_gen
            state["last_good_system"] = current_store
            state["status"] = "HEALTHY"
            save_state_durable(state)
            log("[live-check] Mise à jour à chaud validée sur génération #" + str(current_gen) + ".")
            return 0

        failed_names = [p["name"] + " (" + p["detail"] + ")" for p in probes if not p["ok"]]
        summary = "; ".join(failed_names)
        log("[live-check] ÉCHEC après mise à jour à chaud : " + summary + " -> rollback immédiat !")
        ensure_nix_env()
        subprocess.run(["nixos-rebuild", "switch", "--rollback"], check=False)
        state["status"] = "ROLLED_BACK_LIVE"
        history = state.get("rollback_history", [])
        history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "from_gen": current_gen,
            "to_gen": state.get("last_good_gen"),
            "reason": "Live-check échoué : " + summary,
        })
        state["rollback_history"] = history[-15:]
        save_state_durable(state)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
