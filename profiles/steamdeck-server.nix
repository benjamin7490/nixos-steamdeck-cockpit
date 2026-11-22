# ==============================================================================
# profiles/steamdeck-server.nix — Profil complet Serveur Steam Deck + Cockpit
# ==============================================================================
# Activé automatiquement lorsque vous importez ce dépôt GitHub depuis
# /etc/nixos/configuration.nix :
#
#   imports = [
#     ./hardware-configuration.nix
#     (builtins.fetchTarball "https://github.com/benjamin7490/nixos-steamdeck-cockpit/archive/main.tar.gz")
#   ];
#
# Tous les réglages utilisent lib.mkDefault : vous pouvez surcharger n'importe
# quelle option directement dans votre /etc/nixos/configuration.nix.
# ==============================================================================

{ config, options, pkgs, lib, ... }:

let
  cfgFan = config.services.steamdeck-server;

  steamdeckServerFanDaemon = pkgs.writers.writePython3Bin "steamdeck-server-fan" {
    doCheck = false;
  } ''
    import glob
    import signal
    import sys
    import time
    from pathlib import Path

    # Courbe thermique serveur 24/7 : (Température APU °C, PWM 0..255)
    CURVE = [(40, 60), (50, 95), (60, 140), (70, 195), (80, 240), (85, 255)]
    running = True

    def find_hwmon(names):
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            p = Path(hw)
            nf = p / "name"
            if nf.exists() and nf.read_text(errors="ignore").strip() in names:
                return p
        return None

    def interp(t):
        if t <= CURVE[0][0]:
            return CURVE[0][1]
        for i in range(len(CURVE) - 1):
            t0, p0 = CURVE[i]
            t1, p1 = CURVE[i + 1]
            if t0 <= t <= t1:
                return int(p0 + ((t - t0) / float(t1 - t0)) * (p1 - p0))
        return 255

    def main():
        global running
        def _stop(s, f):
            global running
            running = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        sd = find_hwmon({"steamdeck_hwmon"})
        if not sd:
            return 1
        pwm_en, pwm_val = sd / "pwm1_enable", sd / "pwm1"
        try:
            pwm_en.write_text("1\n")
            while running:
                try:
                    apu = find_hwmon({"k10temp", "amdgpu"})
                    temp = int((apu / "temp1_input").read_text().strip()) / 1000.0 if apu and (apu / "temp1_input").exists() else 65.0
                    pwm_val.write_text(str(interp(temp)) + "\n")
                except Exception as exc:
                    print("[steamdeck-server-fan] Erreur lecture/écriture hwmon : " + str(exc), file=sys.stderr, flush=True)
                time.sleep(2.0)
        finally:
            # Sécurité : rend le contrôle au contrôleur embarqué (BIOS/EC) à l'arrêt
            try:
                pwm_en.write_text("2\n")
            except Exception:
                pass
        return 0

    if __name__ == "__main__":
        sys.exit(main())
  '';

in
{
  imports = [
    ../nixos/cockpit-nixos-module.nix
    (
      (builtins.fetchTarball {
        url = "https://github.com/Jovian-Experiments/Jovian-NixOS/archive/development.tar.gz";
      }) + "/modules"
    )
  ] ++ lib.optional (builtins.pathExists /etc/nixos/cockpit-managed.nix) /etc/nixos/cockpit-managed.nix;

  options.services.steamdeck-server = {
    enableCustomFanCurve = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Remplace jupiter-fan-control (Valve) par une courbe ventilateur serveur 24/7 plus fraîche via steamdeck_hwmon.";
    };

    adminUsers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Utilisateurs existants auxquels ajouter automatiquement les groupes wheel, networkmanager et libvirtd.";
    };
  };

  config = lib.mkMerge [
    {
      # --------------------------------------------------------------------------
      # 1. Écosystème Cockpit Déclaratif + Podman + KVM + Files + Health-Checker
      # --------------------------------------------------------------------------
      services.cockpit-nixos = {
        enable = lib.mkDefault true;
        port = lib.mkDefault 9090;
        enableMachines = lib.mkDefault true;
        enablePodman = lib.mkDefault true;
        enableFiles = lib.mkDefault true;
        enableStorageInterception = lib.mkDefault true;
        enableNetworkInterception = lib.mkDefault true;
        enableFirewallBridge = lib.mkDefault true;
        enablePackageKitBridge = lib.mkDefault true;
        enableAutoUpgrade = lib.mkDefault true;
        autoUpgradeDates = lib.mkDefault "03:30";
        autoUpgradeRebootWindow = lib.mkDefault {
          lower = "03:00";
          upper = "05:30";
        };
        enableHealthChecker = lib.mkDefault true;
        maxBootAttempts = lib.mkDefault 3;
      };

      # Découverte mDNS (.local) sur le LAN
      services.avahi = {
        enable = lib.mkDefault true;
        nssmdns4 = lib.mkDefault true;
      };

      users.users = lib.genAttrs cfgFan.adminUsers (name: {
        extraGroups = [ "wheel" "networkmanager" ]
          ++ lib.optional config.services.cockpit-nixos.enableMachines "libvirtd";
      });

      # --------------------------------------------------------------------------
      # 2. Drivers Steam Deck (ventilateur + matériel) & Tuning Serveur 24/7
      # --------------------------------------------------------------------------
      jovian = {
        devices.steamdeck = {
          enable = lib.mkDefault true;
          autoUpdate = lib.mkDefault false; # Pas de flash BIOS/EC surprise sur un serveur
        };
        steam.enable = lib.mkDefault false; # Mode serveur headless (pas de Gamescope/Steam UI)
      };

      hardware.enableRedistributableFirmware = lib.mkDefault true;
      hardware.cpu.amd.updateMicrocode = lib.mkDefault true;
      nixpkgs.config.allowUnfree = lib.mkDefault true;

      environment.systemPackages = with pkgs; [
        nvme-cli
        powertop
        htop
      ];

      # Drivers EC/hwmon Steam Deck + extinction écran après 60s (anti burn-in OLED)
      boot.kernelModules = [ "steamdeck" "steamdeck_hwmon" "k10temp" ];
      boot.kernelParams = [ "consoleblank=60" ];

      # Empêche toute mise en veille en mode serveur 24/7
      systemd.targets.sleep.enable = lib.mkDefault false;
      systemd.targets.suspend.enable = lib.mkDefault false;
      systemd.targets.hibernate.enable = lib.mkDefault false;
      systemd.targets.hybrid-sleep.enable = lib.mkDefault false;

      # Courbe ventilateur serveur optionnelle (si services.steamdeck-server.enableCustomFanCurve = true)
      systemd.services.steamdeck-server-fan = lib.mkIf cfgFan.enableCustomFanCurve {
        description = "Courbe ventilateur serveur 24/7 via steamdeck_hwmon";
        after = [ "multi-user.target" ];
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "simple";
          ExecStart = "${steamdeckServerFanDaemon}/bin/steamdeck-server-fan";
          Restart = "on-failure";
          RestartSec = "5s";
        };
      };

      systemd.services.jupiter-fan-control = lib.mkIf cfgFan.enableCustomFanCurve {
        enable = lib.mkForce false;
      };
    }

    # Publication mDNS Avahi (compatible NixOS 26.05+/unstable et fallback 24.11/25.05)
    (if (options.services.avahi ? settings) then {
      services.avahi.settings.publish = {
        disable-publishing = lib.mkDefault false;
        publish-addresses = lib.mkDefault true;
        publish-workstation = lib.mkDefault true;
      };
    } else {
      services.avahi.publish = {
        enable = lib.mkDefault true;
        addresses = lib.mkDefault true;
        workstation = lib.mkDefault true;
      };
    })
  ];
}
