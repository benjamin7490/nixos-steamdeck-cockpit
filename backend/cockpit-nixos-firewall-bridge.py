#!/usr/bin/env python3
"""
cockpit-nixos-firewall-bridge : Bridge D-Bus complet (org.fedoraproject.FirewallD1)
reliant directement l'interface native 'Réseau -> Pare-feu' de Cockpit au pare-feu
déclaratif natif NixOS (/etc/nixos/cockpit-managed.nix -> networking.firewall),
tout en restant 100 % compatible avec libvirtd (zone 'libvirt' & interfaces virbr*).

Expose :
  - /org/fedoraproject/FirewallD1          (org.fedoraproject.FirewallD1 + .zone + .policy + .direct)
  - /org/fedoraproject/FirewallD1/config   (org.fedoraproject.FirewallD1.config + Properties.FirewallBackend)
  - /org/fedoraproject/FirewallD1/config/zone/0 (nixos)
  - /org/fedoraproject/FirewallD1/config/zone/1 (libvirt)
  - /org/fedoraproject/FirewallD1/config/service/<idx>

Enregistré avec l'alias systemd `firewalld.service` afin que le module natif
Cockpit Networking détecte et pilote nativement le pare-feu NixOS sans installer
le démon impératif firewalld.
"""

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

STATE_FILE = Path("/var/lib/cockpit-nixos/state.json")
CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl"

BUS_NAME = "org.fedoraproject.FirewallD1"
OBJ_PATH = "/org/fedoraproject/FirewallD1"
CONFIG_PATH = "/org/fedoraproject/FirewallD1/config"
ZONE_NIXOS_PATH = "/org/fedoraproject/FirewallD1/config/zone/0"
ZONE_LIBVIRT_PATH = "/org/fedoraproject/FirewallD1/config/zone/1"

# Catalogue des services standards reconnus par Cockpit Firewall UI
KNOWN_SERVICES = {
    "cockpit":     ("Cockpit", "Interface web d'administration Cockpit", [(9090, "tcp")]),
    "ssh":         ("SSH", "Serveur Secure Shell (OpenSSH)", [(22, "tcp")]),
    "http":        ("HTTP", "Serveur web HTTP", [(80, "tcp")]),
    "https":       ("HTTPS", "Serveur web HTTPS", [(443, "tcp")]),
    "dns":         ("DNS", "Serveur de noms de domaine", [(53, "tcp"), (53, "udp")]),
    "dhcp":        ("DHCP", "Serveur DHCP", [(67, "udp")]),
    "mdns":        ("mDNS", "Découverte réseau local Avahi / mDNS", [(5353, "udp")]),
    "samba":       ("Samba", "Partage de fichiers SMB/CIFS", [(139, "tcp"), (445, "tcp")]),
    "nfs":         ("NFS", "Partage réseau NFS", [(2049, "tcp")]),
    "vnc-server":  ("VNC", "Bureau à distance VNC", [(5900, "tcp")]),
    "wireguard":   ("WireGuard", "VPN WireGuard", [(51820, "udp")]),
    "mqtt":        ("MQTT", "Broker domotique MQTT", [(1883, "tcp")]),
    "syncthing":   ("Syncthing", "Synchronisation de fichiers Syncthing", [(22000, "tcp"), (22000, "udp"), (21027, "udp")]),
    "postgresql":  ("PostgreSQL", "Base de données PostgreSQL", [(5432, "tcp")]),
    "mysql":       ("MySQL / MariaDB", "Base de données MySQL/MariaDB", [(3306, "tcp")]),
    "redis":       ("Redis", "Cache / base clé-valeur Redis", [(6379, "tcp")]),
    "grafana":     ("Grafana", "Tableaux de bord Grafana", [(3000, "tcp")]),
    "prometheus":  ("Prometheus", "Métriques Prometheus", [(9090, "tcp")]),
}

SERVICE_LIST = sorted(KNOWN_SERVICES.keys())

# Interfaces runtime attachées aux zones virtuelles (ex: virbr0 dans 'libvirt')
RUNTIME_ZONE_INTERFACES = {
    "libvirt": ["virbr0"],
    "libvirt-routed": [],
}


def read_fw_state() -> dict:
    if not STATE_FILE.exists():
        return {"enable": True, "allowedTCPPorts": [9090], "allowedUDPPorts": [], "trustedInterfaces": ["podman0", "virbr0"]}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("firewall", {"enable": True, "allowedTCPPorts": [], "allowedUDPPorts": [], "trustedInterfaces": []})
    except Exception:
        return {"enable": True, "allowedTCPPorts": [], "allowedUDPPorts": [], "trustedInterfaces": []}


def ports_as_pairs() -> list:
    fw = read_fw_state()
    out = [(str(p), "tcp") for p in fw.get("allowedTCPPorts", [])]
    out += [(str(p), "udp") for p in fw.get("allowedUDPPorts", [])]
    return out


def active_services() -> list:
    fw = read_fw_state()
    tcp = set(int(p) for p in fw.get("allowedTCPPorts", []))
    udp = set(int(p) for p in fw.get("allowedUDPPorts", []))
    found = []
    for svc in SERVICE_LIST:
        _, _, reqs = KNOWN_SERVICES[svc]
        if all((p in tcp if proto == "tcp" else p in udp) for p, proto in reqs):
            found.append(svc)
    return found


def make_service_settings_v1(svc: str):
    if svc not in KNOWN_SERVICES:
        raise dbus.DBusException("INVALID_SERVICE: " + svc)
    short, desc, reqs = KNOWN_SERVICES[svc]
    ports = dbus.Array([(str(p), proto) for p, proto in reqs], signature="(ss)")
    return (
        dbus.String("1.0"),
        dbus.String(short),
        dbus.String(desc),
        ports,
        dbus.Array([], signature="s"),
        dbus.Dictionary({}, signature="ss"),
        dbus.Array([], signature="s"),
        dbus.Array([], signature="(ss)"),
    )


def make_service_settings_v2(svc: str):
    if svc not in KNOWN_SERVICES:
        raise dbus.DBusException("INVALID_SERVICE: " + svc)
    short, desc, reqs = KNOWN_SERVICES[svc]
    return dbus.Dictionary({
        "version": dbus.String("1.0"),
        "short": dbus.String(short),
        "description": dbus.String(desc),
        "ports": dbus.Array([(str(p), proto) for p, proto in reqs], signature="(ss)"),
        "protocols": dbus.Array([], signature="s"),
        "modules": dbus.Array([], signature="s"),
    }, signature="sv")


def make_zone_settings_v1(zone_name: str = "nixos"):
    if zone_name in ("libvirt", "libvirt-routed"):
        ifaces = dbus.Array(RUNTIME_ZONE_INTERFACES.get(zone_name, []), signature="s")
        return (
            dbus.String("1.0"),
            dbus.String(zone_name),
            dbus.String("Zone virtuelle Libvirt (interfaces virbr* approuvées par NixOS)"),
            dbus.Boolean(False),
            dbus.String("ACCEPT"),
            dbus.Array([], signature="s"),
            dbus.Array([], signature="(ss)"),
            dbus.Array([], signature="s"),
            dbus.Boolean(False),
            dbus.Array([], signature="(ssss)"),
            ifaces,
            dbus.Array([], signature="s"),
            dbus.Array([], signature="s"),
        )
    fw = read_fw_state()
    ifaces = dbus.Array(fw.get("trustedInterfaces", []) or [], signature="s")
    services = dbus.Array(active_services(), signature="s")
    ports = dbus.Array([(p, proto) for p, proto in ports_as_pairs()], signature="(ss)")
    return (
        dbus.String("1.0"),
        dbus.String("NixOS"),
        dbus.String("Pare-feu déclaratif NixOS (/etc/nixos/cockpit-managed.nix)"),
        dbus.Boolean(False),
        dbus.String("default"),
        services,
        ports,
        dbus.Array([], signature="s"),
        dbus.Boolean(False),
        dbus.Array([], signature="(ssss)"),
        ifaces,
        dbus.Array([], signature="s"),
        dbus.Array([], signature="s"),
    )


def make_zone_settings_v2(zone_name: str = "nixos"):
    if zone_name in ("libvirt", "libvirt-routed"):
        return dbus.Dictionary({
            "version": dbus.String("1.0"),
            "short": dbus.String(zone_name),
            "description": dbus.String("Zone virtuelle Libvirt (interfaces virbr* approuvées par NixOS)"),
            "target": dbus.String("ACCEPT"),
            "services": dbus.Array([], signature="s"),
            "ports": dbus.Array([], signature="(ss)"),
            "interfaces": dbus.Array(RUNTIME_ZONE_INTERFACES.get(zone_name, []), signature="s"),
            "sources": dbus.Array([], signature="s"),
            "forward_ports": dbus.Array([], signature="(ssss)"),
            "rich_rules": dbus.Array([], signature="s"),
            "protocols": dbus.Array([], signature="s"),
            "masquerade": dbus.Boolean(False),
        }, signature="sv")
    fw = read_fw_state()
    return dbus.Dictionary({
        "version": dbus.String("1.0"),
        "short": dbus.String("NixOS"),
        "description": dbus.String("Pare-feu déclaratif NixOS (/etc/nixos/cockpit-managed.nix)"),
        "target": dbus.String("default"),
        "services": dbus.Array(active_services(), signature="s"),
        "ports": dbus.Array([(p, proto) for p, proto in ports_as_pairs()], signature="(ss)"),
        "interfaces": dbus.Array(fw.get("trustedInterfaces", []) or [], signature="s"),
        "sources": dbus.Array([], signature="s"),
        "forward_ports": dbus.Array([], signature="(ssss)"),
        "rich_rules": dbus.Array([], signature="s"),
        "protocols": dbus.Array([], signature="s"),
        "masquerade": dbus.Boolean(False),
    }, signature="sv")


def validate_port_proto(port, protocol) -> tuple:
    try:
        p_int = int(str(port).strip())
        if p_int < 1 or p_int > 65535:
            raise ValueError("Port hors plage")
    except ValueError as exc:
        raise dbus.DBusException("INVALID_PORT: " + str(exc))
    proto = str(protocol).lower().strip()
    if proto not in ("tcp", "udp"):
        raise dbus.DBusException("INVALID_PROTOCOL: " + proto)
    return (str(p_int), proto)


def run_ctl_async(args: list, callback=None) -> None:
    def _worker():
        subprocess.run([CTL_BIN] + args, check=False)
        if callback:
            def _once():
                callback()
                return False
            GLib.idle_add(_once)
    threading.Thread(target=_worker, daemon=True).start()


class ConfigServiceObject(dbus.service.Object):
    def __init__(self, bus_name, idx: int, svc: str):
        self.svc = svc
        super().__init__(bus_name, CONFIG_PATH + "/service/" + str(idx))

    @dbus.service.method(BUS_NAME + ".config.service", out_signature="(sssa(ss)asa{ss}asa(ss))")
    def getSettings(self):
        return make_service_settings_v1(self.svc)

    @dbus.service.method(BUS_NAME + ".config.service", out_signature="a{sv}")
    def getSettings2(self):
        return make_service_settings_v2(self.svc)

    @dbus.service.method(BUS_NAME + ".config.service", out_signature="a(ss)")
    def getPorts(self):
        _, _, reqs = KNOWN_SERVICES[self.svc]
        return dbus.Array([(str(p), proto) for p, proto in reqs], signature="(ss)")

    @dbus.service.method(BUS_NAME + ".config.service", out_signature="s")
    def getShort(self):
        return KNOWN_SERVICES[self.svc][0]

    @dbus.service.method(BUS_NAME + ".config.service", out_signature="s")
    def getDescription(self):
        return KNOWN_SERVICES[self.svc][1]


class ConfigZoneObject(dbus.service.Object):
    def __init__(self, bus_name, root_obj, zone_name: str, obj_path: str):
        self.root_obj = root_obj
        self.zone_name = zone_name
        super().__init__(bus_name, obj_path)

    @dbus.service.method(BUS_NAME + ".config.zone", out_signature="s")
    def getName(self):
        return self.zone_name

    @dbus.service.method(BUS_NAME + ".config.zone", out_signature="(sssbsasa(ss)asba(ssss)asasas)")
    def getSettings(self):
        return make_zone_settings_v1(self.zone_name)

    @dbus.service.method(BUS_NAME + ".config.zone", out_signature="a{sv}")
    def getSettings2(self):
        return make_zone_settings_v2(self.zone_name)

    @dbus.service.method(BUS_NAME + ".config.zone", out_signature="as")
    def getServices(self):
        if self.zone_name != "nixos":
            return dbus.Array([], signature="s")
        return dbus.Array(active_services(), signature="s")

    @dbus.service.method(BUS_NAME + ".config.zone", out_signature="a(ss)")
    def getPorts(self):
        if self.zone_name != "nixos":
            return dbus.Array([], signature="(ss)")
        return dbus.Array([(p, proto) for p, proto in ports_as_pairs()], signature="(ss)")

    @dbus.service.method(BUS_NAME + ".config.zone", out_signature="as")
    def getInterfaces(self):
        if self.zone_name in RUNTIME_ZONE_INTERFACES:
            return dbus.Array(RUNTIME_ZONE_INTERFACES[self.zone_name], signature="s")
        return dbus.Array(read_fw_state().get("trustedInterfaces", []) or [], signature="s")

    @dbus.service.method(BUS_NAME + ".config.zone", in_signature="s")
    def addService(self, service):
        self.root_obj.addService(self.zone_name, service, 0)

    @dbus.service.method(BUS_NAME + ".config.zone", in_signature="s")
    def removeService(self, service):
        self.root_obj.removeService(self.zone_name, service)

    @dbus.service.method(BUS_NAME + ".config.zone", in_signature="ss")
    def addPort(self, port, protocol):
        self.root_obj.addPort(self.zone_name, port, protocol, 0)

    @dbus.service.method(BUS_NAME + ".config.zone", in_signature="ss")
    def removePort(self, port, protocol):
        self.root_obj.removePort(self.zone_name, port, protocol)

    @dbus.service.signal(BUS_NAME + ".config.zone", signature="s")
    def Updated(self, name):
        pass


class ConfigRootObject(dbus.service.Object):
    def __init__(self, bus_name):
        super().__init__(bus_name, CONFIG_PATH)

    @dbus.service.method(BUS_NAME + ".config", out_signature="as")
    def getZoneNames(self):
        return dbus.Array(["nixos", "libvirt"], signature="s")

    @dbus.service.method(BUS_NAME + ".config", out_signature="ao")
    def listZones(self):
        return dbus.Array([dbus.ObjectPath(ZONE_NIXOS_PATH), dbus.ObjectPath(ZONE_LIBVIRT_PATH)], signature="o")

    @dbus.service.method(BUS_NAME + ".config", in_signature="s", out_signature="o")
    def getZoneByName(self, zone):
        if str(zone) in ("libvirt", "libvirt-routed"):
            return dbus.ObjectPath(ZONE_LIBVIRT_PATH)
        return dbus.ObjectPath(ZONE_NIXOS_PATH)

    @dbus.service.method(BUS_NAME + ".config", out_signature="as")
    def getServiceNames(self):
        return dbus.Array(SERVICE_LIST, signature="s")

    @dbus.service.method(BUS_NAME + ".config", out_signature="ao")
    def listServices(self):
        return dbus.Array(
            [dbus.ObjectPath(CONFIG_PATH + "/service/" + str(i)) for i in range(len(SERVICE_LIST))],
            signature="o",
        )

    @dbus.service.method(BUS_NAME + ".config", in_signature="s", out_signature="o")
    def getServiceByName(self, service):
        s = str(service)
        if s not in KNOWN_SERVICES:
            raise dbus.DBusException("INVALID_SERVICE: " + s)
        idx = SERVICE_LIST.index(s)
        return dbus.ObjectPath(CONFIG_PATH + "/service/" + str(idx))

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        if str(property_name) == "FirewallBackend":
            return dbus.String("nftables" if shutil.which("nft") else "iptables")
        if str(property_name) == "DefaultZone":
            return dbus.String("nixos")
        return dbus.String("")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        return dbus.Dictionary({
            "FirewallBackend": dbus.String("nftables" if shutil.which("nft") else "iptables"),
            "DefaultZone": dbus.String("nixos"),
        }, signature="sv")


class NixOSFirewallDShim(dbus.service.Object):
    def __init__(self, bus_name):
        self.zone_cfg = None
        super().__init__(bus_name, OBJ_PATH)

    def _notify_zone_updated(self):
        if self.zone_cfg:
            self.zone_cfg.Updated("nixos")

    @dbus.service.method(BUS_NAME, out_signature="s")
    def getDefaultZone(self):
        return "nixos"

    @dbus.service.method(BUS_NAME, out_signature="as")
    def listServices(self):
        return dbus.Array(SERVICE_LIST, signature="s")

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="(sssa(ss)asa{ss}asa(ss))")
    def getServiceSettings(self, service):
        return make_service_settings_v1(str(service))

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="a{sv}")
    def getServiceSettings2(self, service):
        return make_service_settings_v2(str(service))

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="(sssbsasa(ss)asba(ssss)asasas)")
    def getZoneSettings(self, zone):
        return make_zone_settings_v1(str(zone) or "nixos")

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="a{sv}")
    def getZoneSettings2(self, zone):
        return make_zone_settings_v2(str(zone) or "nixos")

    @dbus.service.method(BUS_NAME)
    def authorizeAll(self):
        return None

    @dbus.service.method(BUS_NAME)
    def runtimeToPermanent(self):
        return None

    @dbus.service.method(BUS_NAME)
    def reload(self):
        run_ctl_async(["rebuild"], lambda: self.Reloaded())

    # --- Interface org.fedoraproject.FirewallD1.zone ---
    @dbus.service.method(BUS_NAME + ".zone", out_signature="as")
    def getZones(self):
        return dbus.Array(["nixos", "libvirt", "libvirt-routed"], signature="s")

    @dbus.service.method(BUS_NAME + ".zone", out_signature="a{sa{sas}}")
    def getActiveZones(self):
        ifaces = read_fw_state().get("trustedInterfaces", []) or []
        out = {
            "nixos": {"interfaces": dbus.Array(ifaces, signature="s"), "sources": dbus.Array([], signature="s")},
        }
        if RUNTIME_ZONE_INTERFACES.get("libvirt"):
            out["libvirt"] = {
                "interfaces": dbus.Array(RUNTIME_ZONE_INTERFACES["libvirt"], signature="s"),
                "sources": dbus.Array([], signature="s"),
            }
        return out

    @dbus.service.method(BUS_NAME + ".zone", in_signature="s", out_signature="s")
    def getZoneOfInterface(self, interface):
        iface = str(interface)
        for z, lst in RUNTIME_ZONE_INTERFACES.items():
            if iface in lst:
                return z
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ss", out_signature="s")
    def addInterface(self, zone, interface):
        return self.changeZoneOfInterface(zone, interface)

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ss", out_signature="s")
    def changeZoneOfInterface(self, zone, interface):
        z = str(zone) or "nixos"
        iface = str(interface).strip()
        for lst in RUNTIME_ZONE_INTERFACES.values():
            if iface in lst:
                lst.remove(iface)
        if z in RUNTIME_ZONE_INTERFACES and iface:
            RUNTIME_ZONE_INTERFACES[z].append(iface)
        return z

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ss", out_signature="s")
    def removeInterface(self, zone, interface):
        iface = str(interface).strip()
        removed_from = str(zone) or "nixos"
        for z, lst in RUNTIME_ZONE_INTERFACES.items():
            if iface in lst:
                lst.remove(iface)
                removed_from = z
        return removed_from

    @dbus.service.method(BUS_NAME + ".zone", in_signature="s", out_signature="aas")
    def getPorts(self, zone):
        if str(zone) in ("libvirt", "libvirt-routed"):
            return dbus.Array([], signature="as")
        return dbus.Array([dbus.Array([p, proto], signature="s") for p, proto in ports_as_pairs()], signature="as")

    @dbus.service.method(BUS_NAME + ".zone", in_signature="s", out_signature="as")
    def getServices(self, zone):
        if str(zone) in ("libvirt", "libvirt-routed"):
            return dbus.Array([], signature="s")
        return dbus.Array(active_services(), signature="s")

    @dbus.service.method(BUS_NAME + ".zone", in_signature="s", out_signature="as")
    def getInterfaces(self, zone):
        z = str(zone)
        if z in RUNTIME_ZONE_INTERFACES:
            return dbus.Array(RUNTIME_ZONE_INTERFACES[z], signature="s")
        return dbus.Array(read_fw_state().get("trustedInterfaces", []) or [], signature="s")

    @dbus.service.method(BUS_NAME + ".zone", in_signature="sssu", out_signature="s")
    def addPort(self, zone, port, protocol, timeout):
        p, proto = validate_port_proto(port, protocol)
        def _done():
            self.PortAdded("nixos", p, proto, 0)
            self._notify_zone_updated()
        run_ctl_async(["fw-port-add", p, proto], _done)
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="sss", out_signature="s")
    def removePort(self, zone, port, protocol):
        p, proto = validate_port_proto(port, protocol)
        def _done():
            self.PortRemoved("nixos", p, proto)
            self._notify_zone_updated()
        run_ctl_async(["fw-port-remove", p, proto], _done)
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ssu", out_signature="s")
    def addService(self, zone, service, timeout):
        svc = str(service)
        if svc not in KNOWN_SERVICES:
            raise dbus.DBusException("INVALID_SERVICE: " + svc)
        _, _, reqs = KNOWN_SERVICES[svc]
        payload = json.dumps([{"port": p, "proto": proto} for p, proto in reqs])
        def _done():
            self.ServiceAdded("nixos", svc, 0)
            self._notify_zone_updated()
        run_ctl_async(["fw-ports-batch", "add", payload], _done)
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ss", out_signature="s")
    def removeService(self, zone, service):
        svc = str(service)
        if svc not in KNOWN_SERVICES:
            raise dbus.DBusException("INVALID_SERVICE: " + svc)
        _, _, reqs = KNOWN_SERVICES[svc]
        payload = json.dumps([{"port": p, "proto": proto} for p, proto in reqs])
        def _done():
            self.ServiceRemoved("nixos", svc)
            self._notify_zone_updated()
        run_ctl_async(["fw-ports-batch", "remove", payload], _done)
        return "nixos"

    # --- Interface org.fedoraproject.FirewallD1.policy (requis par libvirt >= 9) ---
    @dbus.service.method(BUS_NAME + ".policy", out_signature="as")
    def getPolicies(self):
        return dbus.Array(["libvirt-to-host", "libvirt-routed-in", "libvirt-routed-out"], signature="s")

    # --- Interface org.fedoraproject.FirewallD1.direct (compatibilité libvirt backend iptables) ---
    @dbus.service.method(BUS_NAME + ".direct", in_signature="sas", out_signature="s")
    def passthrough(self, ipv, args):
        bin_map = {"ipv4": "iptables", "ipv6": "ip6tables", "eb": "ebtables"}
        tool = bin_map.get(str(ipv))
        if not tool or not shutil.which(tool):
            return ""
        res = subprocess.run([tool] + [str(a) for a in args], capture_output=True, text=True)
        if res.returncode != 0:
            raise dbus.DBusException("COMMAND_FAILED: " + (res.stderr or res.stdout).strip())
        return res.stdout

    @dbus.service.signal(BUS_NAME + ".zone", signature="sssi")
    def PortAdded(self, zone, port, protocol, timeout):
        pass

    @dbus.service.signal(BUS_NAME + ".zone", signature="sss")
    def PortRemoved(self, zone, port, protocol):
        pass

    @dbus.service.signal(BUS_NAME + ".zone", signature="ssi")
    def ServiceAdded(self, zone, service, timeout):
        pass

    @dbus.service.signal(BUS_NAME + ".zone", signature="ss")
    def ServiceRemoved(self, zone, service):
        pass

    @dbus.service.signal(BUS_NAME)
    def Reloaded(self):
        pass

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        if str(property_name) == "state":
            return dbus.String("RUNNING")
        if str(property_name) == "version":
            return dbus.String("1.3.0-nixos")
        return dbus.String("")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        return {
            "state": dbus.String("RUNNING"),
            "version": dbus.String("1.3.0-nixos"),
        }


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    bus_name = dbus.service.BusName(BUS_NAME, bus=bus)
    root = NixOSFirewallDShim(bus_name)
    ConfigRootObject(bus_name)
    root.zone_cfg = ConfigZoneObject(bus_name, root, "nixos", ZONE_NIXOS_PATH)
    ConfigZoneObject(bus_name, root, "libvirt", ZONE_LIBVIRT_PATH)
    for idx, svc in enumerate(SERVICE_LIST):
        ConfigServiceObject(bus_name, idx, svc)
    print("[firewall-bridge] Bridge D-Bus complet org.fedoraproject.FirewallD1 -> NixOS (+ zones libvirt) actif.", flush=True)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
