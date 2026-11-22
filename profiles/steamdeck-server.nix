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

  # Courbe ventilateur serveur 24/7 utilisant la VRAIE API sysfs de steamdeck_hwmon
  # (fan1_target en RPM 2000..7300, compatible Steam Deck LCD Jupiter & OLED Galileo,
  # et retour automatique au contrôle EC via fan1_target = 0 à l'arrêt).
  steamdeckServerFanDaemon = pkgs.writers.writePython3Bin "steamdeck-server-fan" {
    doCheck = false;
  } ''
    import glob
    import signal
    import sys
    import time
    from pathlib import Path

    # Courbe thermique serveur 24/7 : (Température max °C, Vitesse cible RPM)
    # Plage officielle Valve (jupiter-fan-control) : 1500/2000..7300 RPM
    CURVE = [
        (35.0, 2000),
        (45.0, 2600),
        (55.0, 3400),
        (65.0, 4500),
        (75.0, 5800),
        (82.0, 7000),
        (86.0, 7300),
    ]
    running = True

    def find_hwmons(names):
        found = []
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            p = Path(hw)
            nf = p / "name"
            if nf.exists() and nf.read_text(errors="ignore").strip() in names:
                found.append(p)
        return found

    def read_max_temp_c():
        temps = []
        for hw in find_hwmons({"k10temp", "amdgpu", "acpitz"}):
            for tf in sorted(hw.glob("temp*_input")):
                try:
                    val = int(tf.read_text(encoding="utf-8").strip()) / 1000.0
                    if 0.0 < val < 150.0:
                        temps.append(val)
                except Exception:
                    pass
        return max(temps) if temps else None

    def interp_rpm(temp_c):
        if temp_c is None:
            return 5500  # Valeur refuge sûre si capteur temporairement illisible
        if temp_c <= CURVE[0][0]:
            return CURVE[0][1]
        for i in range(len(CURVE) - 1):
            t0, r0 = CURVE[i]
            t1, r1 = CURVE[i + 1]
            if t0 <= temp_c <= t1:
                return int(r0 + ((temp_c - t0) / float(t1 - t0)) * (r1 - r0))
        return CURVE[-1][1]

    def main():
        global running
        def _stop(s, f):
            global running
            running = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # Attend jusqu'à 10s que le module steamdeck_hwmon / jupiter soit initialisé
        sd = None
        for _ in range(20):
            matches = find_hwmons({"steamdeck_hwmon", "jupiter"})
            if matches and (matches[0] / "fan1_target").exists():
                sd = matches[0]
                break
            time.sleep(0.5)

        if not sd:
            # Pas sur un Steam Deck (ex. VM de test) : quitte proprement sans échec systemd
            print("[steamdeck-server-fan] steamdeck_hwmon/fan1_target non détecté (matériel non-Steam Deck), arrêt propre.", flush=True)
            return 0

        fan_target = sd / "fan1_target"
        last_rpm = 0
        print("[steamdeck-server-fan] Contrôle ventilateur serveur 24/7 actif sur " + str(fan_target), flush=True)
        try:
            while running:
                try:
                    temp = read_max_temp_c()
                    rpm = interp_rpm(temp)
                    # Hystérésis douce (+/- 150 RPM) sauf en montée thermique rapide
                    if rpm > last_rpm or abs(rpm - last_rpm) >= 150 or last_rpm == 0:
                        fan_target.write_text(str(rpm) + "\n", encoding="utf-8")
                        last_rpm = rpm
                except Exception as exc:
                    print("[steamdeck-server-fan] Erreur lecture/écriture hwmon : " + str(exc), file=sys.stderr, flush=True)
                time.sleep(2.0)
        finally:
            # Sécurité Valve officielle : écrire 0 dans fan1_target rend le contrôle à l'EC
            try:
                fan_target.write_text("0\n", encoding="utf-8")
            except Exception:
                pass
        return 0

    if __name__ == "__main__":
        sys.exit(main())
  '';

  # Script utilitaire pour couper ou rallumer physiquement la dalle + rétroéclairage (LCD & OLED)
  steamdeckScreenCtl = pkgs.writeShellScriptBin "steamdeck-screen" ''
    set -eu
    action="''${1:-off}"
    if [ "$action" = "on" ]; then
      for bl in /sys/class/backlight/*; do
        [ -d "$bl" ] || continue
        [ -w "$bl/bl_power" ] && echo 0 > "$bl/bl_power" || true
        if [ -r "$bl/max_brightness" ] && [ -w "$bl/brightness" ]; then
          max=$(cat "$bl/max_brightness")
          echo $((max / 2 > 0 ? max / 2 : max)) > "$bl/brightness" || true
        fi
      done
      for fb in /sys/class/graphics/fb*/blank; do
        [ -w "$fb" ] && echo 0 > "$fb" || true
      done
    else
      for bl in /sys/class/backlight/*; do
        [ -d "$bl" ] || continue
        [ -w "$bl/brightness" ] && echo 0 > "$bl/brightness" || true
        [ -w "$bl/bl_power" ] && echo 4 > "$bl/bl_power" || true
      done
      # Contrôleur rétroéclairage OLED Galileo (i2c-ANX7580A) si présent
      oled_bl="/sys/devices/platform/AMDI0010:02/i2c-2/i2c-ANX7580A:00/brightness"
      [ -w "$oled_bl" ] && echo 0 > "$oled_bl" || true
      for fb in /sys/class/graphics/fb*/blank; do
        [ -w "$fb" ] && echo 4 > "$fb" || true
      done
    fi
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
      description = "Remplace jupiter-fan-control (Valve) par une courbe ventilateur serveur 24/7 plus fraîche via steamdeck_hwmon (fan1_target RPM).";
    };

    adminUsers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Utilisateurs existants auxquels ajouter automatiquement les groupes wheel, networkmanager et libvirtd.";
    };

    screenAutoOff = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active DPMS powerdown + extinction physique du rétroéclairage et de la dalle 60s après le démarrage (anti burn-in OLED & zéro lumière LCD).";
    };

    disableInternalDisplay = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Désactive totalement la dalle interne du Steam Deck au niveau noyau (video=eDP-1:d) dès la sortie du bootloader (un écran externe USB-C/HDMI reste utilisable).";
    };
  };

  config = lib.mkMerge [
    {
      # --------------------------------------------------------------------------
      # 1. Écosystème Cockpit Déclaratif + Podman + KVM + Stockage + Health-Checker
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
      # 2. Drivers Steam Deck (kernel Neptune + ventilateur) & Tuning Serveur 24/7
      # --------------------------------------------------------------------------
      jovian = {
        devices.steamdeck = {
          enable = lib.mkDefault true;
          autoUpdate = lib.mkDefault false; # Pas de flash BIOS/EC surprise sur un serveur
          # Désactive proprement jupiter-fan-control si la courbe serveur custom est choisie
          enableOsFanControl = lib.mkIf cfgFan.enableCustomFanCurve (lib.mkForce false);
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
        steamdeckScreenCtl
      ];

      # Extinction console après 60s + niveau de log console réduit (loglevel=3)
      # + désactivation matérielle eDP-1 optionnelle (video=eDP-1:d)
      # (les drivers steamdeck/steamdeck_hwmon/k10temp sont auto-chargés par le kernel Neptune via ACPI/PCI)
      boot.consoleLogLevel = lib.mkDefault 3;
      boot.kernelParams =
        [ "consoleblank=60" ]
        ++ lib.optional cfgFan.disableInternalDisplay "video=eDP-1:d";

      # Active le vrai mode DPMS powerdown sur la console TTY1 (au lieu d'un simple écran noir rétroéclairé)
      systemd.services.steamdeck-screen-powersave = lib.mkIf (cfgFan.screenAutoOff && !cfgFan.disableInternalDisplay) {
        description = "Configure DPMS powerdown sur la console Steam Deck";
        wantedBy = [ "multi-user.target" ];
        unitConfig.ConditionVirtualization = "!vm";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${pkgs.writeShellScript "steamdeck-tty-powersave" ''
            [ -e /dev/tty1 ] && ${pkgs.util-linux}/bin/setterm --blank 1 --powersave powerdown --powerdown 1 < /dev/tty1 > /dev/tty1 || true
          ''}";
        };
      };

      # Coupe physiquement le rétroéclairage & la dalle 60s après le démarrage (LCD & OLED)
      systemd.services.steamdeck-backlight-off = lib.mkIf (cfgFan.screenAutoOff && !cfgFan.disableInternalDisplay) {
        description = "Éteint physiquement le rétroéclairage et la dalle Steam Deck (anti burn-in)";
        unitConfig.ConditionVirtualization = "!vm";
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${steamdeckScreenCtl}/bin/steamdeck-screen off";
        };
      };

      systemd.timers.steamdeck-backlight-off = lib.mkIf (cfgFan.screenAutoOff && !cfgFan.disableInternalDisplay) {
        description = "Minuterie d'extinction de l'écran Steam Deck 60s après le boot";
        wantedBy = [ "timers.target" ];
        unitConfig.ConditionVirtualization = "!vm";
        timerConfig = {
          OnBootSec = "60s";
          AccuracySec = "5s";
          Unit = "steamdeck-backlight-off.service";
        };
      };

      # Empêche toute mise en veille en mode serveur 24/7
      systemd.targets.sleep.enable = lib.mkDefault false;
      systemd.targets.suspend.enable = lib.mkDefault false;
      systemd.targets.hibernate.enable = lib.mkDefault false;
      systemd.targets.hybrid-sleep.enable = lib.mkDefault false;

      # Courbe ventilateur serveur optionnelle (si services.steamdeck-server.enableCustomFanCurve = true)
      systemd.services.steamdeck-server-fan = lib.mkIf cfgFan.enableCustomFanCurve {
        description = "Courbe ventilateur serveur 24/7 via steamdeck_hwmon (fan1_target RPM)";
        after = [ "multi-user.target" ];
        wantedBy = [ "multi-user.target" ];
        unitConfig.ConditionVirtualization = "!vm";
        serviceConfig = {
          Type = "simple";
          ExecStart = "${steamdeckServerFanDaemon}/bin/steamdeck-server-fan";
          OOMScoreAdjust = -1000;
          Restart = "on-failure";
          RestartSec = "5s";
        };
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
