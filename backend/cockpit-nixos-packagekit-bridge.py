#!/usr/bin/env python3
"""
cockpit-nixos-packagekit-bridge : Bridge D-Bus (org.freedesktop.PackageKit)
qui rend le VRAI onglet natif 'Mises à jour logicielles' (Software Updates) de
Cockpit 100 % fonctionnel et déclaratif sur NixOS.

Traduit les transactions PackageKit de Cockpit :
  - RefreshCache    -> nix-channel --update + calcul du diff de closure NixOS
  - GetUpdates      -> liste les mises à jour disponibles (paquets / système NixOS)
  - GetUpdateDetail -> renvoie les détails de version avant -> après
  - UpdatePackages  -> exécute nixos-rebuild switch --upgrade (+ RequireRestart si noyau changé)
                       (gère aussi les drapeaux SIMULATE et ONLY_DOWNLOAD)
  - SearchNames / Resolve / InstallPackages / RemovePackages / GetPackages
                    -> cockpit-nixos-ctl pkg-add / pkg-remove (/etc/nixos/cockpit-managed.nix)
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS_NAME = "org.freedesktop.PackageKit"
ROOT_PATH = "/org/freedesktop/PackageKit"
OFFLINE_IFACE = "org.freedesktop.PackageKit.Offline"
TX_IFACE = "org.freedesktop.PackageKit.Transaction"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl"
CACHE_DIR = Path("/var/lib/cockpit-nixos")
UPDATES_CACHE = CACHE_DIR / "pk-updates-cache.json"
LOCK_FILE = Path("/run/lock/cockpit-nixos.lock")
REBUILD_SENTINEL = Path("/run/cockpit-nixos/rebuilding")

# Constantes PackageKit 1.x (libpackagekit-glib2 / pk-enum.h)
PK_EXIT_SUCCESS = 1
PK_EXIT_FAILED = 2
PK_EXIT_CANCELLED = 3

PK_ROLE_UNKNOWN = 0
PK_ROLE_GET_DETAILS = 4
PK_ROLE_GET_UPDATE_DETAIL = 8
PK_ROLE_GET_UPDATES = 9
PK_ROLE_INSTALL_PACKAGES = 10
PK_ROLE_REFRESH_CACHE = 13
PK_ROLE_REMOVE_PACKAGES = 14
PK_ROLE_RESOLVE = 16
PK_ROLE_SEARCH_NAME = 19
PK_ROLE_UPDATE_PACKAGES = 22
PK_ROLE_GET_PACKAGES = 25

PK_INFO_INSTALLED = 1
PK_INFO_AVAILABLE = 2
PK_INFO_NORMAL = 5
PK_INFO_ENHANCEMENT = 7
PK_INFO_SECURITY = 8
PK_INFO_INSTALLING = 11
PK_INFO_REMOVING = 12
PK_INFO_UPDATING = 13

PK_STATUS_WAIT = 1
PK_STATUS_SETUP = 2
PK_STATUS_RUNNING = 3
PK_STATUS_QUERY = 4
PK_STATUS_INFO = 5
PK_STATUS_REMOVE = 6
PK_STATUS_REFRESH_CACHE = 7
PK_STATUS_DOWNLOAD = 8
PK_STATUS_INSTALL = 9
PK_STATUS_UPDATE = 10
PK_STATUS_FINISHED = 18

PK_RESTART_NONE = 1
PK_RESTART_SYSTEM = 4
PK_UPDATE_STATE_STABLE = 1
PK_NETWORK_ONLINE = 2
PK_AUTHORIZE_YES = 1

PK_ERROR_INTERNAL = 2
PK_ERROR_PACKAGE_NOT_FOUND = 11

PK_TX_FLAG_ONLY_TRUSTED = 1 << 1
PK_TX_FLAG_SIMULATE = 1 << 2
PK_TX_FLAG_ONLY_DOWNLOAD = 1 << 3

_CACHE_LOCK = threading.Lock()


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


def with_rebuild_lock(fn):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def load_cached_updates() -> dict:
    if not UPDATES_CACHE.exists():
        return {"timestamp": 0, "updates": []}
    try:
        return json.loads(UPDATES_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {"timestamp": 0, "updates": []}


def save_cached_updates(updates: list) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPDATES_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"timestamp": int(time.time()), "updates": updates}, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, UPDATES_CACHE)


def compute_nixos_updates() -> list:
    """Évalue la configuration avec le canal actuel et compare avec /run/current-system."""
    with _CACHE_LOCK:
        ensure_nix_env()
        eval_res = subprocess.run(
            ["nixos-rebuild", "build", "--no-out-link"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if eval_res.returncode != 0:
            return []

        target_path = ""
        for line in reversed((eval_res.stdout or "").splitlines()):
            s = line.strip()
            if s.startswith("/nix/store/"):
                target_path = s
                break
        if not target_path or not Path(target_path).exists():
            return []

        current_sys = os.path.realpath("/run/current-system")
        if os.path.realpath(target_path) == current_sys:
            return []

        diff_res = subprocess.run(
            ["nix", "--extra-experimental-features", "nix-command", "store", "diff-closures", current_sys, target_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        updates = []
        if diff_res.returncode == 0:
            for line in diff_res.stdout.splitlines():
                # Format : "pkg: 1.0 -> 1.1, +12 KiB"
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
                info = (
                    PK_INFO_SECURITY
                    if any(k in name.lower() for k in ("linux", "openssl", "openssh", "sudo", "polkit", "systemd", "glibc"))
                    else PK_INFO_NORMAL
                )
                pkg_id = name + ";" + new_v + ";x86_64;nixos"
                updates.append({
                    "id": pkg_id,
                    "name": name,
                    "old_version": old_v,
                    "new_version": new_v,
                    "info": info,
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
                "info": PK_INFO_NORMAL,
                "summary": "Nouvelle génération système NixOS (" + new_ver + ")",
            })
        return updates


class PackageKitTransaction(dbus.service.Object):
    def __init__(self, bus_name, root_obj, tx_path: str):
        self.root_obj = root_obj
        self.tx_path = tx_path
        self.start_time = time.time()
        self.cancelled = False
        self.props = {
            "Role": dbus.UInt32(PK_ROLE_UNKNOWN),
            "Status": dbus.UInt32(PK_STATUS_SETUP),
            "LastPackage": dbus.String(""),
            "Uid": dbus.UInt32(0),
            "Percentage": dbus.UInt32(101),
            "AllowCancel": dbus.Boolean(True),
            "CallerActive": dbus.Boolean(True),
            "ElapsedTime": dbus.UInt32(0),
            "RemainingTime": dbus.UInt32(0),
            "Speed": dbus.UInt32(0),
            "DownloadSizeRemaining": dbus.UInt64(0),
            "TransactionFlags": dbus.UInt64(0),
        }
        super().__init__(bus_name, tx_path)

    def _emit_idle(self, fn):
        def _once():
            fn()
            return False
        GLib.idle_add(_once)

    def _emit_error(self, code: int, details: str):
        msg = str(details)
        self._emit_idle(lambda c=code, m=msg: self.ErrorCode(dbus.UInt32(c), dbus.String(m)))

    def _set_props(self, **kwargs):
        changed = {}
        for k, v in kwargs.items():
            self.props[k] = v
            changed[k] = v
        snap = dict(changed)
        self._emit_idle(
            lambda s=snap: self.PropertiesChanged(
                TX_IFACE,
                dbus.Dictionary(s, signature="sv"),
                dbus.Array([], signature="s"),
            )
        )

    def _finish(self, exit_code: int = PK_EXIT_SUCCESS):
        runtime_ms = int((time.time() - self.start_time) * 1000)
        self._set_props(Status=dbus.UInt32(PK_STATUS_FINISHED), Percentage=dbus.UInt32(100))

        def _done():
            self.Finished(dbus.UInt32(exit_code), dbus.UInt32(runtime_ms))
            self.Destroy()
            GLib.timeout_add_seconds(5, self._cleanup)

        self._emit_idle(_done)

    def _cleanup(self):
        try:
            self.remove_from_connection()
        except Exception:
            pass
        return False

    def _spawn(self, target):
        threading.Thread(target=target, daemon=True).start()

    @dbus.service.method(TX_IFACE, in_signature="as")
    def SetHints(self, hints):
        return None

    @dbus.service.method(TX_IFACE)
    def Cancel(self):
        self.cancelled = True

    @dbus.service.method(TX_IFACE, in_signature="b")
    def RefreshCache(self, force):
        def _work():
            try:
                ensure_nix_env()
                self._set_props(
                    Role=dbus.UInt32(PK_ROLE_REFRESH_CACHE),
                    Status=dbus.UInt32(PK_STATUS_REFRESH_CACHE),
                    Percentage=dbus.UInt32(15),
                )
                subprocess.run(["nix-channel", "--update"], capture_output=True, text=True, timeout=300)
                self._set_props(Status=dbus.UInt32(PK_STATUS_QUERY), Percentage=dbus.UInt32(55))
                updates = compute_nixos_updates()
                save_cached_updates(updates)
                self._emit_idle(lambda: self.root_obj.UpdatesChanged())
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="t")
    def GetUpdates(self, filter_flags):
        def _work():
            try:
                self._set_props(
                    Role=dbus.UInt32(PK_ROLE_GET_UPDATES),
                    Status=dbus.UInt32(PK_STATUS_QUERY),
                    Percentage=dbus.UInt32(25),
                )
                cache = load_cached_updates()
                updates = cache.get("updates", [])
                if int(time.time()) - int(cache.get("timestamp", 0)) > 3600:
                    updates = compute_nixos_updates()
                    save_cached_updates(updates)
                for u in updates:
                    self._emit_idle(
                        lambda item=u: self.Package(
                            dbus.UInt32(item.get("info", PK_INFO_NORMAL)),
                            dbus.String(item["id"]),
                            dbus.String(item.get("summary", "")),
                        )
                    )
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="as")
    def GetUpdateDetail(self, package_ids):
        def _work():
            self._set_props(Role=dbus.UInt32(PK_ROLE_GET_UPDATE_DETAIL), Status=dbus.UInt32(PK_STATUS_INFO))
            cache = load_cached_updates()
            by_id = {u["id"]: u for u in cache.get("updates", [])}
            for pid in package_ids:
                s_pid = str(pid)
                u = by_id.get(s_pid, {})
                old_v = u.get("old_version", "current")
                new_v = u.get("new_version", "latest")
                name = u.get("name", s_pid.split(";")[0])
                restart = (
                    PK_RESTART_SYSTEM
                    if any(k in name.lower() for k in ("linux", "systemd", "nixos-system"))
                    else PK_RESTART_NONE
                )
                text = "Mise à jour déclarative NixOS : " + name + " (" + old_v + " -> " + new_v + ")."
                self._emit_idle(
                    lambda p=s_pid, r=restart, t=text: self.UpdateDetail(
                        dbus.String(p),
                        dbus.Array([], signature="s"),
                        dbus.Array([], signature="s"),
                        dbus.Array([], signature="s"),
                        dbus.Array([], signature="s"),
                        dbus.Array([], signature="s"),
                        dbus.UInt32(r),
                        dbus.String(t),
                        dbus.String(""),
                        dbus.UInt32(PK_UPDATE_STATE_STABLE),
                        dbus.String(""),
                        dbus.String(""),
                    )
                )
            self._finish(PK_EXIT_SUCCESS)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="as")
    def GetDetails(self, package_ids):
        def _work():
            self._set_props(Role=dbus.UInt32(PK_ROLE_GET_DETAILS), Status=dbus.UInt32(PK_STATUS_INFO))
            for pid in package_ids:
                s_pid = str(pid)
                name = s_pid.split(";")[0]
                details = dbus.Dictionary(
                    {
                        "package-id": dbus.String(s_pid),
                        "summary": dbus.String("Paquet NixOS déclaratif : " + name),
                        "description": dbus.String("Géré via /etc/nixos/cockpit-managed.nix"),
                        "url": dbus.String("https://search.nixos.org/packages?query=" + name),
                        "size": dbus.UInt64(0),
                    },
                    signature="sv",
                )
                self._emit_idle(lambda d=details: self.Details(d))
            self._finish(PK_EXIT_SUCCESS)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="tas")
    def UpdatePackages(self, transaction_flags, package_ids):
        def _work():
            flags = int(transaction_flags)
            try:
                ensure_nix_env()
                self._set_props(
                    Role=dbus.UInt32(PK_ROLE_UPDATE_PACKAGES),
                    TransactionFlags=dbus.UInt64(flags),
                    Status=dbus.UInt32(PK_STATUS_UPDATE),
                    Percentage=dbus.UInt32(20),
                )

                if flags & PK_TX_FLAG_SIMULATE:
                    cache = load_cached_updates()
                    by_id = {u["id"]: u for u in cache.get("updates", [])}
                    ids = [str(p) for p in package_ids] or list(by_id.keys())
                    for pid in ids:
                        summary = by_id.get(pid, {}).get("summary", "Mise à jour NixOS")
                        self._emit_idle(
                            lambda p=pid, s=summary: self.Package(
                                dbus.UInt32(PK_INFO_UPDATING),
                                dbus.String(p),
                                dbus.String(s),
                            )
                        )
                    self._finish(PK_EXIT_SUCCESS)
                    return

                if flags & PK_TX_FLAG_ONLY_DOWNLOAD:
                    self._set_props(Status=dbus.UInt32(PK_STATUS_DOWNLOAD), Percentage=dbus.UInt32(40))
                    res = with_rebuild_lock(
                        lambda: subprocess.run(
                            ["nixos-rebuild", "build", "--upgrade", "--no-out-link"],
                            capture_output=True,
                            text=True,
                        )
                    )
                    if res.returncode != 0:
                        err = (res.stderr or res.stdout or "Échec du pré-téléchargement nixos-rebuild")[-600:]
                        self._emit_error(PK_ERROR_INTERNAL, err)
                        self._finish(PK_EXIT_FAILED)
                        return
                    self._finish(PK_EXIT_SUCCESS)
                    return

                booted_k = (
                    os.path.realpath("/run/booted-system/kernel")
                    if Path("/run/booted-system/kernel").exists()
                    else ""
                )

                def _do_upgrade():
                    REBUILD_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
                    REBUILD_SENTINEL.write_text(str(os.getpid()), encoding="utf-8")
                    try:
                        return subprocess.run(
                            ["nixos-rebuild", "switch", "--upgrade"],
                            capture_output=True,
                            text=True,
                        )
                    finally:
                        REBUILD_SENTINEL.unlink(missing_ok=True)

                res = with_rebuild_lock(_do_upgrade)
                if res.returncode != 0:
                    err = (res.stderr or res.stdout or "Échec de nixos-rebuild switch --upgrade")[-600:]
                    self._emit_error(PK_ERROR_INTERNAL, err)
                    self._finish(PK_EXIT_FAILED)
                    return

                save_cached_updates([])
                next_k = (
                    os.path.realpath("/nix/var/nix/profiles/system/kernel")
                    if Path("/nix/var/nix/profiles/system/kernel").exists()
                    else ""
                )
                if booted_k and next_k and booted_k != next_k:
                    self._emit_idle(
                        lambda: self.RequireRestart(
                            dbus.UInt32(PK_RESTART_SYSTEM),
                            dbus.String("nixos-system;latest;x86_64;nixos"),
                        )
                    )
                self._emit_idle(lambda: self.root_obj.UpdatesChanged())
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="tas")
    def SearchNames(self, filter_flags, values):
        def _work():
            try:
                self._set_props(
                    Role=dbus.UInt32(PK_ROLE_SEARCH_NAME),
                    Status=dbus.UInt32(PK_STATUS_QUERY),
                    Percentage=dbus.UInt32(30),
                )
                query = " ".join(str(v) for v in values).strip()
                res = subprocess.run([CTL_BIN, "pkg-search", query], capture_output=True, text=True, timeout=60)
                if res.returncode == 0:
                    for item in json.loads(res.stdout or "[]"):
                        pid = item["attr"] + ";nixpkgs;x86_64;nixos"
                        desc = item.get("description") or item.get("pname", "")
                        self._emit_idle(
                            lambda p=pid, d=desc: self.Package(
                                dbus.UInt32(PK_INFO_AVAILABLE),
                                dbus.String(p),
                                dbus.String(d),
                            )
                        )
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="tas")
    def Resolve(self, filter_flags, packages):
        def _work():
            self._set_props(Role=dbus.UInt32(PK_ROLE_RESOLVE), Status=dbus.UInt32(PK_STATUS_QUERY))
            for pkg in packages:
                name = str(pkg).strip()
                pid = name + ";nixpkgs;x86_64;nixos"
                self._emit_idle(
                    lambda p=pid, n=name: self.Package(
                        dbus.UInt32(PK_INFO_AVAILABLE),
                        dbus.String(p),
                        dbus.String("Paquet NixOS : " + n),
                    )
                )
            self._finish(PK_EXIT_SUCCESS)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="tas")
    def InstallPackages(self, transaction_flags, package_ids):
        def _work():
            flags = int(transaction_flags)
            try:
                self._set_props(
                    Role=dbus.UInt32(PK_ROLE_INSTALL_PACKAGES),
                    TransactionFlags=dbus.UInt64(flags),
                    Status=dbus.UInt32(PK_STATUS_INSTALL),
                    Percentage=dbus.UInt32(20),
                )
                if flags & PK_TX_FLAG_SIMULATE:
                    for pid in package_ids:
                        s_pid = str(pid)
                        self._emit_idle(
                            lambda p=s_pid: self.Package(
                                dbus.UInt32(PK_INFO_INSTALLING),
                                dbus.String(p),
                                dbus.String("Installation déclarative NixOS"),
                            )
                        )
                    self._finish(PK_EXIT_SUCCESS)
                    return

                for pid in package_ids:
                    attr = str(pid).split(";")[0].strip()
                    res = subprocess.run([CTL_BIN, "pkg-add", attr], capture_output=True, text=True)
                    if res.returncode != 0:
                        self._emit_error(PK_ERROR_INTERNAL, (res.stderr or res.stdout)[-500:])
                        self._finish(PK_EXIT_FAILED)
                        return
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="tasbb")
    def RemovePackages(self, transaction_flags, package_ids, allow_deps, autoremove):
        def _work():
            flags = int(transaction_flags)
            try:
                self._set_props(
                    Role=dbus.UInt32(PK_ROLE_REMOVE_PACKAGES),
                    TransactionFlags=dbus.UInt64(flags),
                    Status=dbus.UInt32(PK_STATUS_REMOVE),
                    Percentage=dbus.UInt32(20),
                )
                if flags & PK_TX_FLAG_SIMULATE:
                    for pid in package_ids:
                        s_pid = str(pid)
                        self._emit_idle(
                            lambda p=s_pid: self.Package(
                                dbus.UInt32(PK_INFO_REMOVING),
                                dbus.String(p),
                                dbus.String("Suppression déclarative NixOS"),
                            )
                        )
                    self._finish(PK_EXIT_SUCCESS)
                    return

                for pid in package_ids:
                    attr = str(pid).split(";")[0].strip()
                    res = subprocess.run([CTL_BIN, "pkg-remove", attr], capture_output=True, text=True)
                    if res.returncode != 0:
                        self._emit_error(PK_ERROR_INTERNAL, (res.stderr or res.stdout)[-500:])
                        self._finish(PK_EXIT_FAILED)
                        return
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.method(TX_IFACE, in_signature="t")
    def GetPackages(self, filter_flags):
        def _work():
            try:
                self._set_props(Role=dbus.UInt32(PK_ROLE_GET_PACKAGES), Status=dbus.UInt32(PK_STATUS_QUERY))
                res = subprocess.run([CTL_BIN, "status"], capture_output=True, text=True)
                if res.returncode == 0:
                    pkgs = json.loads(res.stdout).get("state", {}).get("packages", [])
                    for p in pkgs:
                        pid = p + ";managed;x86_64;nixos"
                        self._emit_idle(
                            lambda x=pid, n=p: self.Package(
                                dbus.UInt32(PK_INFO_INSTALLED),
                                dbus.String(x),
                                dbus.String("Déclaré dans cockpit-managed.nix : " + n),
                            )
                        )
                self._finish(PK_EXIT_SUCCESS)
            except Exception as exc:
                self._emit_error(PK_ERROR_INTERNAL, str(exc))
                self._finish(PK_EXIT_FAILED)

        self._spawn(_work)

    @dbus.service.signal(TX_IFACE, signature="uss")
    def Package(self, info, package_id, summary):
        pass

    @dbus.service.signal(TX_IFACE, signature="a{sv}")
    def Details(self, data):
        pass

    @dbus.service.signal(TX_IFACE, signature="sasasasasasussuss")
    def UpdateDetail(self, package_id, updates, obsoletes, vendor_urls, bugzilla_urls, cve_urls, restart, update_text, changelog, state, issued, updated):
        pass

    @dbus.service.signal(TX_IFACE, signature="us")
    def RequireRestart(self, restart_type, package_id):
        pass

    @dbus.service.signal(TX_IFACE, signature="us")
    def ErrorCode(self, code, details):
        pass

    @dbus.service.signal(TX_IFACE, signature="uu")
    def Finished(self, exit_code, runtime):
        pass

    @dbus.service.signal(TX_IFACE)
    def Destroy(self):
        pass

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface_name, changed_properties, invalidated_properties):
        pass

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        return self.props.get(str(property_name), dbus.String(""))

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        return dbus.Dictionary(self.props, signature="sv")

    @dbus.service.method(PROPS_IFACE, in_signature="ssv")
    def Set(self, interface_name, property_name, value):
        return None


class NixOSPackageKitDaemon(dbus.service.Object):
    def __init__(self, bus_name):
        self.bus_name = bus_name
        self.tx_counter = 0
        self.props = {
            "VersionMajor": dbus.UInt32(1),
            "VersionMinor": dbus.UInt32(2),
            "VersionMicro": dbus.UInt32(8),
            "BackendName": dbus.String("nixos"),
            "BackendDescription": dbus.String("NixOS Declarative PackageKit Bridge"),
            "BackendAuthor": dbus.String("nixos-steamdeck-cockpit"),
            "Roles": dbus.UInt64(0xFFFFFFFF),
            "Groups": dbus.UInt64(0),
            "Filters": dbus.UInt64(0),
            "MimeTypes": dbus.Array([], signature="s"),
            "Locked": dbus.Boolean(False),
            "NetworkState": dbus.UInt32(PK_NETWORK_ONLINE),
            "DistroId": dbus.String("nixos;unstable;x86_64"),
        }
        self.offline_props = {
            "UpdatePrepared": dbus.Boolean(False),
            "UpdateTriggered": dbus.Boolean(False),
            "UpgradePrepared": dbus.Boolean(False),
            "UpgradeTriggered": dbus.Boolean(False),
            "TriggerAction": dbus.String("none"),
        }
        super().__init__(bus_name, ROOT_PATH)

    @dbus.service.method(BUS_NAME, out_signature="o")
    def CreateTransaction(self):
        self.tx_counter += 1
        tx_path = ROOT_PATH + "/Transaction/" + str(self.tx_counter)
        PackageKitTransaction(self.bus_name, self, tx_path)
        return dbus.ObjectPath(tx_path)

    @dbus.service.method(BUS_NAME, in_signature="u", out_signature="u")
    def GetTimeSinceAction(self, role):
        ts = int(load_cached_updates().get("timestamp", 0))
        if ts <= 0:
            return dbus.UInt32(86400)
        return dbus.UInt32(max(0, int(time.time()) - ts))

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="u")
    def CanAuthorize(self, action_id):
        return dbus.UInt32(PK_AUTHORIZE_YES)

    @dbus.service.method(BUS_NAME, in_signature="s")
    def StateHasChanged(self, reason):
        self.UpdatesChanged()

    @dbus.service.method(BUS_NAME)
    def SuggestDaemonQuit(self):
        return None

    @dbus.service.method(OFFLINE_IFACE)
    def ClearResults(self):
        return None

    @dbus.service.method(OFFLINE_IFACE)
    def Cancel(self):
        return None

    @dbus.service.signal(BUS_NAME)
    def UpdatesChanged(self):
        pass

    @dbus.service.signal(BUS_NAME)
    def RepoListChanged(self):
        pass

    @dbus.service.signal(BUS_NAME)
    def RestartSchedule(self):
        pass

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        if str(interface_name) == OFFLINE_IFACE:
            return self.offline_props.get(str(property_name), dbus.Boolean(False))
        return self.props.get(str(property_name), dbus.String(""))

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if str(interface_name) == OFFLINE_IFACE:
            return dbus.Dictionary(self.offline_props, signature="sv")
        return dbus.Dictionary(self.props, signature="sv")

    @dbus.service.method(PROPS_IFACE, in_signature="ssv")
    def Set(self, interface_name, property_name, value):
        return None


def main() -> int:
    ensure_nix_env()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    bus_name = dbus.service.BusName(BUS_NAME, bus=bus)
    NixOSPackageKitDaemon(bus_name)
    print("[packagekit-bridge] Bridge D-Bus org.freedesktop.PackageKit -> NixOS actif.", flush=True)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
