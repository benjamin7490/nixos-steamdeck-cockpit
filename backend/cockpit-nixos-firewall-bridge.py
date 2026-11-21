#!/usr/bin/env python3
"""
cockpit-nixos-firewall-bridge : Shim D-Bus léger (org.fedoraproject.FirewallD1)
qui intercepte les actions de la page native 'Networking -> Firewall' de Cockpit
et les redirige vers /etc/nixos/cockpit-managed.nix (networking.firewall) +
nixos-rebuild switch via cockpit-nixos-ctl.

Permet de conserver le pare-feu natif déclaratif NixOS (nftables/iptables) sans
installer le démon firewalld impératif.
"""

import json
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
ZONE_CONFIG_PATH = "/org/fedoraproject/FirewallD1/config/zone/0"

KNOWN_SERVICES = {
    "ssh": [(22, "tcp")],
    "cockpit": [(9090, "tcp")],
    "http": [(80, "tcp")],
    "https": [(443, "tcp")],
    "dns": [(53, "tcp"), (53, "udp")],
    "samba": [(139, "tcp"), (445, "tcp")],
    "nfs": [(2049, "tcp")],
    "vnc-server": [(5900, "tcp")],
    "mdns": [(5353, "udp")],
    "wireguard": [(51820, "udp")],
    "mqtt": [(1883, "tcp")],
}


def read_fw_state() -> dict:
    if not STATE_FILE.exists():
        return {"enable": True, "allowedTCPPorts": [9090], "allowedUDPPorts": [], "trustedInterfaces": []}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return data.get("firewall", {"enable": True, "allowedTCPPorts": [], "allowedUDPPorts": [], "trustedInterfaces": []})


def ports_as_dbus_array() -> list:
    fw = read_fw_state()
    out = []
    for p in fw.get("allowedTCPPorts", []):
        out.append((str(p), "tcp"))
    for p in fw.get("allowedUDPPorts", []):
        out.append((str(p), "udp"))
    return out


def active_services() -> list:
    fw = read_fw_state()
    tcp = set(int(p) for p in fw.get("allowedTCPPorts", []))
    udp = set(int(p) for p in fw.get("allowedUDPPorts", []))
    found = []
    for svc, reqs in KNOWN_SERVICES.items():
        if all((p in tcp if proto == "tcp" else p in udp) for p, proto in reqs):
            found.append(svc)
    return found


def run_ctl_async(args: list, callback=None) -> None:
    def _worker():
        subprocess.run([CTL_BIN] + args, check=False)
        if callback:
            GLib.idle_add(callback)
    threading.Thread(target=_worker, daemon=True).start()


class NixOSFirewallDShim(dbus.service.Object):
    def __init__(self, bus):
        bus_name = dbus.service.BusName(BUS_NAME, bus=bus)
        super().__init__(bus_name, OBJ_PATH)

    @dbus.service.method(BUS_NAME, out_signature="s")
    def getDefaultZone(self):
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", out_signature="as")
    def getZones(self):
        return ["nixos"]

    @dbus.service.method(BUS_NAME + ".zone", out_signature="a{sa{sas}}")
    def getActiveZones(self):
        fw = read_fw_state()
        ifaces = fw.get("trustedInterfaces", []) or ["default"]
        return {"nixos": {"interfaces": dbus.Array(ifaces, signature="s")}}

    @dbus.service.method(BUS_NAME, out_signature="as")
    def listServices(self):
        return sorted(KNOWN_SERVICES.keys())

    @dbus.service.method(BUS_NAME + ".zone", in_signature="s", out_signature="aas")
    def getPorts(self, zone):
        return dbus.Array([dbus.Array([p, proto], signature="s") for p, proto in ports_as_dbus_array()], signature="as")

    @dbus.service.method(BUS_NAME + ".zone", in_signature="s", out_signature="as")
    def getServices(self, zone):
        return dbus.Array(active_services(), signature="s")

    @dbus.service.method(BUS_NAME + ".zone", in_signature="sssu", out_signature="s")
    def addPort(self, zone, port, protocol, timeout):
        p = str(int(port))
        proto = str(protocol).lower()
        run_ctl_async(["fw-port-add", p, proto], lambda: self.PortAdded("nixos", p, proto, 0))
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="sss", out_signature="s")
    def removePort(self, zone, port, protocol):
        p = str(int(port))
        proto = str(protocol).lower()
        run_ctl_async(["fw-port-remove", p, proto], lambda: self.PortRemoved("nixos", p, proto))
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ssu", out_signature="s")
    def addService(self, zone, service, timeout):
        reqs = KNOWN_SERVICES.get(str(service), [])
        def _apply():
            for p, proto in reqs:
                subprocess.run([CTL_BIN, "fw-port-add", str(p), proto], check=False)
            GLib.idle_add(lambda: self.ServiceAdded("nixos", str(service), 0))
        threading.Thread(target=_apply, daemon=True).start()
        return "nixos"

    @dbus.service.method(BUS_NAME + ".zone", in_signature="ss", out_signature="s")
    def removeService(self, zone, service):
        reqs = KNOWN_SERVICES.get(str(service), [])
        def _apply():
            for p, proto in reqs:
                subprocess.run([CTL_BIN, "fw-port-remove", str(p), proto], check=False)
            GLib.idle_add(lambda: self.ServiceRemoved("nixos", str(service)))
        threading.Thread(target=_apply, daemon=True).start()
        return "nixos"

    @dbus.service.method(BUS_NAME)
    def reload(self):
        run_ctl_async(["rebuild"], lambda: self.Reloaded())

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
        if property_name == "state":
            return dbus.String("RUNNING")
        if property_name == "version":
            return dbus.String("nixos-declarative-bridge-1.0")
        return dbus.String("")

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        return {
            "state": dbus.String("RUNNING"),
            "version": dbus.String("nixos-declarative-bridge-1.0"),
        }


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    NixOSFirewallDShim(bus)
    print("[firewall-bridge] Bridge D-Bus org.fedoraproject.FirewallD1 -> NixOS actif.", flush=True)
    loop = GLib.MainLoop()
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
