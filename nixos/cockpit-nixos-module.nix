{ config, options, pkgs, lib, ... }:

let
  cfg = config.services.cockpit-nixos;

  # 1. Backend privilégié transactionnel (cockpit-nixos-ctl)
  cockpitNixosCtl = pkgs.writers.writePython3Bin "cockpit-nixos-ctl" {
    doCheck = false;
  } (builtins.readFile ../backend/cockpit-nixos-ctl.py);

  # 2. Intercepteur NetworkManager -> /etc/nixos/cockpit-managed.nix
  cockpitNixosNetIntercept = pkgs.writers.writePython3Bin "cockpit-nixos-net-intercept" {
    doCheck = false;
  } (builtins.readFile ../backend/cockpit-nixos-net-intercept.py);

  # 3. Health-Checker & Auto-Rollback (style openSUSE MicroOS)
  nixosHealthChecker = pkgs.writers.writePython3Bin "nixos-health-checker" {
    doCheck = false;
  } (builtins.readFile ../backend/nixos-health-checker.py);

  # 4. Bridge D-Bus Pare-feu (org.fedoraproject.FirewallD1 -> networking.firewall)
  cockpitNixosFirewallBridge = pkgs.writers.writePython3Bin "cockpit-nixos-firewall-bridge" {
    doCheck = false;
    libraries = with pkgs.python3Packages; [
      dbus-python
      pygobject3
    ];
  } (builtins.readFile ../backend/cockpit-nixos-firewall-bridge.py);

  # 5. Paquet D-Bus policy pour autoriser le bridge FirewallD1 (restreint à root + wheel)
  firewallBridgeDbusPolicy = pkgs.writeTextDir "share/dbus-1/system.d/org.fedoraproject.FirewallD1.conf" ''
    <!DOCTYPE busconfig PUBLIC
     "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
     "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
    <busconfig>
      <policy user="root">
        <allow own="org.fedoraproject.FirewallD1"/>
        <allow send_destination="org.fedoraproject.FirewallD1"/>
      </policy>
      <policy group="wheel">
        <allow send_destination="org.fedoraproject.FirewallD1"/>
      </policy>
      <policy context="default">
        <deny own="org.fedoraproject.FirewallD1"/>
        <deny send_destination="org.fedoraproject.FirewallD1"
              send_interface="org.fedoraproject.FirewallD1"
              send_member="reload"/>
        <deny send_destination="org.fedoraproject.FirewallD1"
              send_interface="org.fedoraproject.FirewallD1.zone"
              send_member="addPort"/>
        <deny send_destination="org.fedoraproject.FirewallD1"
              send_interface="org.fedoraproject.FirewallD1.zone"
              send_member="removePort"/>
        <deny send_destination="org.fedoraproject.FirewallD1"
              send_interface="org.fedoraproject.FirewallD1.zone"
              send_member="addService"/>
        <deny send_destination="org.fedoraproject.FirewallD1"
              send_interface="org.fedoraproject.FirewallD1.zone"
              send_member="removeService"/>
      </policy>
    </busconfig>
  '';

  # 6. Dérivation du plugin Frontend Cockpit (exposé dans /etc/cockpit/share/cockpit & /run/current-system/sw/share/cockpit)
  cockpitNixosPlugin = pkgs.runCommand "cockpit-nixos-plugin" { } ''
    mkdir -p $out/share/cockpit/nixos-manager
    cp ${../plugin/manifest.json} $out/share/cockpit/nixos-manager/manifest.json
    cp ${../plugin/index.html}    $out/share/cockpit/nixos-manager/index.html
    cp ${../plugin/style.css}     $out/share/cockpit/nixos-manager/style.css
    cp ${../plugin/app.js}        $out/share/cockpit/nixos-manager/app.js
  '';

  # Liste des plugins Cockpit à enregistrer
  cockpitPluginPackages =
    [ cockpitNixosPlugin ]
    ++ lib.optionals cfg.enableFiles (lib.optional (pkgs ? cockpit-files) pkgs.cockpit-files)
    ++ lib.optional cfg.enablePodman pkgs.cockpit-podman
    ++ lib.optional cfg.enableMachines pkgs.cockpit-machines;

  # Origines par défaut (corrige nixpkgs#453337 pour http/https localhost, tunnel SSH et accès LAN/.local)
  defaultAllowedOrigins =
    let
      p = toString cfg.port;
      host = config.networking.hostName;
    in
    [
      "https://localhost:${p}"
      "http://localhost:${p}"
      "https://127.0.0.1:${p}"
      "http://127.0.0.1:${p}"
      "https://192.168.*:${p}"
      "http://192.168.*:${p}"
      "https://10.*:${p}"
      "http://10.*:${p}"
      "https://172.*:${p}"
      "http://172.*:${p}"
      "https://100.*:${p}"
      "http://100.*:${p}"
      "https://*.local:${p}"
      "http://*.local:${p}"
    ]
    ++ lib.optionals (host != "") [
      "https://${host}:${p}"
      "http://${host}:${p}"
      "https://${host}.local:${p}"
      "http://${host}.local:${p}"
    ]
    ++ cfg.allowedOrigins;

in
{
  options.services.cockpit-nixos = {
    enable = lib.mkEnableOption "Plugin déclaratif Cockpit pour NixOS (réseau, pare-feu, paquets, générations, montages)";

    port = lib.mkOption {
      type = lib.types.port;
      default = 9090;
      description = "Port TCP d'écoute de Cockpit.";
    };

    allowedOrigins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Origines supplémentaires autorisées dans /etc/cockpit/cockpit.conf (WebService.Origins). Laisser vide pour autoriser automatiquement toutes les IP/hostnames locaux.";
    };

    enableMachines = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active cockpit-machines + KVM/QEMU + libvirtd + libvirt-dbus.";
    };

    enablePodman = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active cockpit-podman + virtualisation.podman + podman.socket.";
    };

    enableFiles = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active cockpit-files + udisks2 + dossiers montés (/srv/data, /mnt).";
    };

    enableNetworkInterception = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Intercepte les modifications NetworkManager pour les écrire dans /etc/nixos/cockpit-managed.nix.";
    };

    enableFirewallBridge = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active le bridge D-Bus org.fedoraproject.FirewallD1 -> networking.firewall.";
    };

    enableAutoUpgrade = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active les mises à jour automatiques NixOS (system.autoUpgrade) avec fenêtre de redémarrage.";
    };

    autoUpgradeDates = lib.mkOption {
      type = lib.types.str;
      default = "03:30";
      description = "Planification systemd OnCalendar des mises à jour automatiques.";
    };

    autoUpgradeRebootWindow = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {
        lower = "03:00";
        upper = "05:30";
      };
      description = "Plage horaire autorisée pour les redémarrages automatiques après mise à jour.";
    };

    enableHealthChecker = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Active le health-checker au démarrage et le rollback automatique (style openSUSE MicroOS).";
    };

    maxBootAttempts = lib.mkOption {
      type = lib.types.int;
      default = 3;
      description = "Nombre maximal de démarrages échoués/boucles avant rollback automatique sur l'ancienne génération saine.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      # -------------------------------------------------------------------------
      # 1. Service Cockpit & découverte des plugins dans /share/cockpit
      # -------------------------------------------------------------------------
    services.cockpit = {
      enable = true;
      port = cfg.port;
      openFirewall = true;
      settings = {
        WebService = {
          AllowUnencrypted = false;
        };
      };
    };

    # Expose /run/current-system/sw/share/cockpit pour tous les plugins
    environment.pathsToLink = [ "/share/cockpit" ];

    # Masque l'onglet natif PackageKit (incompatible NixOS) via JSON Merge Patch officiel
    environment.etc."cockpit/packagekit.override.json".text = builtins.toJSON {
      menu = { updates = null; };
      tools = { updates = null; };
    };

    # -------------------------------------------------------------------------
    # 2. Paquets de l'écosystème Cockpit + outils backend
    # -------------------------------------------------------------------------
    environment.systemPackages = with pkgs; [
      cockpit
      cockpitNixosPlugin
      cockpitNixosCtl
      cockpitNixosNetIntercept
      nixosHealthChecker
      acl
      lm_sensors
      lsof
      jq
    ]
    ++ lib.optionals cfg.enableFiles (
      lib.optional (pkgs ? cockpit-files) pkgs.cockpit-files
      ++ [ udisks2 fuse3 nfs-utils cifs-utils ]
    )
    ++ lib.optionals cfg.enablePodman [
      cockpit-podman
      podman-compose
      skopeo
    ]
    ++ lib.optionals cfg.enableMachines [
      cockpit-machines
      libvirt-dbus
      virt-manager
      virt-viewer
      dnsmasq
    ];

    # -------------------------------------------------------------------------
    # 3. Initialisation sûre de /etc/nixos/cockpit-managed.nix & dossiers Cockpit-Files
    # -------------------------------------------------------------------------
    system.activationScripts.initCockpitManaged = lib.stringAfter [ "etc" ] ''
      mkdir -p /etc/nixos /var/lib/cockpit-nixos/backups /run/cockpit-nixos
      if [ ! -f /etc/nixos/cockpit-managed.nix ]; then
        printf '# Fichier initialisé automatiquement par cockpit-nixos\n{ config, pkgs, lib, ... }:\n{\n}\n' > /etc/nixos/cockpit-managed.nix
        chmod 0644 /etc/nixos/cockpit-managed.nix
      fi
    '';

    # Répertoires prêts pour Cockpit-Files, VMs & Health-Checker (permissions groupe wheel)
    systemd.tmpfiles.rules = [
      "d /var/lib/cockpit-nixos 0755 root wheel -"
      "d /var/lib/cockpit-nixos/backups 0750 root wheel -"
      "d /srv/data 2775 root wheel -"
      "d /srv/shares 2775 root wheel -"
      "d /mnt/storage 2775 root wheel -"
    ] ++ lib.optionals cfg.enableMachines [
      "d /var/lib/libvirt/images 2775 root libvirtd -"
    ] ++ lib.optionals cfg.enableHealthChecker [
      "d /var/lib/nixos-health-checker 0755 root root -"
    ];

    # Gestion des disques / montages amovibles pour Cockpit-Files & Storage
    services.udisks2.enable = lib.mkDefault cfg.enableFiles;

    # -------------------------------------------------------------------------
    # 4. Réseau (NetworkManager) + Pare-feu natif NixOS
    # -------------------------------------------------------------------------
    networking.networkmanager.enable = lib.mkDefault true;
    networking.firewall.enable = lib.mkDefault true;

    # Watchers d'interception NetworkManager -> /etc/nixos/cockpit-managed.nix
    systemd.paths.cockpit-nixos-net-intercept = lib.mkIf cfg.enableNetworkInterception {
      description = "Surveille les modifications NetworkManager (Cockpit Networking)";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathChanged = "/etc/NetworkManager/system-connections";
        Unit = "cockpit-nixos-net-intercept.service";
      };
    };

    systemd.services.cockpit-nixos-net-intercept = lib.mkIf cfg.enableNetworkInterception {
      description = "Synchronise les profils NetworkManager modifiés vers /etc/nixos/cockpit-managed.nix";
      after = [ "NetworkManager.service" ];
      path = with pkgs; [ networkmanager nix nixos-rebuild coreutils systemd ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${cockpitNixosNetIntercept}/bin/cockpit-nixos-net-intercept";
      };
    };

    # Bridge D-Bus org.fedoraproject.FirewallD1 -> networking.firewall (NixOS)
    services.dbus.packages =
      lib.optional cfg.enableFirewallBridge firewallBridgeDbusPolicy
      ++ lib.optional cfg.enableMachines pkgs.libvirt-dbus;

    systemd.services.cockpit-nixos-firewall-bridge = lib.mkIf cfg.enableFirewallBridge {
      description = "Bridge déclaratif D-Bus FirewallD1 -> NixOS networking.firewall";
      after = [ "dbus.service" "network.target" ];
      wantedBy = [ "multi-user.target" ];
      path = with pkgs; [ nix nixos-rebuild coreutils systemd ];
      environment = {
        GI_TYPELIB_PATH = lib.makeSearchPath "lib/girepository-1.0" [
          pkgs.glib.out
          pkgs.gobject-introspection
        ];
      };
      serviceConfig = {
        Type = "simple";
        ExecStart = "${cockpitNixosFirewallBridge}/bin/cockpit-nixos-firewall-bridge";
        Restart = "on-failure";
        RestartSec = "3s";
      };
    };

    # -------------------------------------------------------------------------
    # 5. Virtualisation KVM/Libvirt (cockpit-machines) & Podman (cockpit-podman)
    # -------------------------------------------------------------------------
    virtualisation.libvirtd = lib.mkIf cfg.enableMachines {
      enable = true;
      qemu = {
        package = pkgs.qemu_kvm;
        runAsRoot = true;
        swtpm.enable = true;
      };
    };

    virtualisation.podman = lib.mkIf cfg.enablePodman {
      enable = true;
      dockerCompat = true;
      dockerSocket.enable = true;
      defaultNetwork.settings.dns_enabled = true;
      autoPrune = {
        enable = true;
        dates = "weekly";
      };
    };

    # -------------------------------------------------------------------------
    # 6. Sécurité : Règles Polkit & Sudo calibrées au moindre privilège
    # -------------------------------------------------------------------------
    security.polkit.enable = true;
    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (!subject.isInGroup("wheel")) {
          return polkit.Result.NOT_HANDLED;
        }
        var allowedPrefixes = [
          "org.freedesktop.systemd1.manage-units",
          "org.freedesktop.NetworkManager.",
          "org.freedesktop.udisks2.",
          "org.libvirt.unix.manage",
          "org.fedoraproject.FirewallD1."
        ];
        for (var i = 0; i < allowedPrefixes.length; i++) {
          if (action.id.indexOf(allowedPrefixes[i]) === 0) {
            return polkit.Result.YES;
          }
        }
      });
    '';

    # Sudo : NOPASSWD uniquement sur le binaire immuable cockpit-nixos-ctl (dans le Nix store)
    security.sudo.enable = true;
    security.sudo.extraRules = [
      {
        groups = [ "wheel" ];
        commands = [
          {
            command = "${cockpitNixosCtl}/bin/cockpit-nixos-ctl";
            options = [ "NOPASSWD" "NOSETENV" ];
          }
          {
            command = "/run/current-system/sw/bin/cockpit-nixos-ctl";
            options = [ "NOPASSWD" "NOSETENV" ];
          }
        ];
      }
    ];

    # -------------------------------------------------------------------------
    # 7. Mises à jour automatiques + Health-Checker & Auto-Rollback (style MicroOS)
    # -------------------------------------------------------------------------
    system.autoUpgrade = lib.mkIf cfg.enableAutoUpgrade {
      enable = true;
      dates = cfg.autoUpgradeDates;
      randomizedDelaySec = "30min";
      allowReboot = true;
      rebootWindow = cfg.autoUpgradeRebootWindow;
      persistent = true;
    };

    # Nettoyage périodique prudent (conserve les 14 derniers jours pour les rollbacks)
    nix.gc = lib.mkIf cfg.enableAutoUpgrade {
      automatic = lib.mkDefault true;
      dates = lib.mkDefault "weekly";
      options = lib.mkDefault "--delete-older-than 14d";
    };

    # Kernel panic -> redémarrage automatique au bout de 10s (permet au boot counter de compter)
    boot.kernelParams = lib.optionals cfg.enableHealthChecker [ "panic=10" "oops=panic" ];

    # Phase 1 (précoce) : compteur de démarrages résistant aux boot-loops/crashes
    systemd.services.nixos-boot-counter = lib.mkIf cfg.enableHealthChecker {
      description = "Compteur précoce de démarrages NixOS (anti boot-loop style MicroOS)";
      wantedBy = [ "sysinit.target" ];
      after = [ "local-fs.target" ];
      before = [ "basic.target" "network-pre.target" "nixos-health-checker.service" ];
      unitConfig.DefaultDependencies = false;
      path = with pkgs; [ nix nixos-rebuild coreutils systemd ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${nixosHealthChecker}/bin/nixos-health-checker --boot-counter --max-attempts ${toString cfg.maxBootAttempts} --cockpit-port ${toString cfg.port}";
      };
    };

    # Phase 2 (boot-complete.target) : sondes système + bénédiction ou rollback automatique
    systemd.services.nixos-health-checker = lib.mkIf cfg.enableHealthChecker {
      description = "Vérification de santé système NixOS (style openSUSE MicroOS health-checker)";
      after = [ "nixos-boot-counter.service" "network-online.target" "dbus.service" "cockpit.socket" ]
        ++ lib.optional cfg.enablePodman "podman.socket"
        ++ lib.optional cfg.enableMachines "libvirtd.service";
      wants = [ "network-online.target" ];
      before = [ "boot-complete.target" ];
      wantedBy = [ "multi-user.target" ];
      requiredBy = [ "boot-complete.target" ];
      path = with pkgs; [ nix nixos-rebuild iproute2 coreutils systemd ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${nixosHealthChecker}/bin/nixos-health-checker --verify --max-attempts ${toString cfg.maxBootAttempts} --cockpit-port ${toString cfg.port}";
      };
    };

    # Phase 3 (post-upgrade à chaud sans reboot) : vérifie les services et rollback live si cassé
    systemd.services.nixos-upgrade = lib.mkIf (cfg.enableAutoUpgrade && cfg.enableHealthChecker) {
      path = [ pkgs.iproute2 ];
      serviceConfig.ExecStartPost = [
        "${nixosHealthChecker}/bin/nixos-health-checker --live-check --cockpit-port ${toString cfg.port}"
      ];
    };
  }

  # Enregistrement natif des plugins Cockpit (NixOS 25.11+/unstable)
  (lib.optionalAttrs (options.services.cockpit ? plugins) {
    services.cockpit.plugins = cockpitPluginPackages;
  })

  # Origines autorisées (corrige nixpkgs#453337 : tunnel SSH http://localhost, 127.0.0.1, LAN, .local)
  (if (options.services.cockpit ? allowed-origins) then {
    services.cockpit.allowed-origins = defaultAllowedOrigins;
  } else {
    services.cockpit.settings.WebService.Origins = lib.mkForce (lib.concatStringsSep " " defaultAllowedOrigins);
  })

  # Watchdog matériel systemd (compatible NixOS 25.05+/unstable et fallback 24.11)
  (if (options.systemd ? settings && options.systemd.settings ? Manager) then {
    systemd.settings.Manager = lib.mkIf cfg.enableHealthChecker {
      RuntimeWatchdogSec = lib.mkDefault "30s";
      RebootWatchdogSec = lib.mkDefault "2min";
    };
  } else {
    systemd.watchdog = lib.mkIf cfg.enableHealthChecker {
      runtimeTime = lib.mkDefault "30s";
      rebootTime = lib.mkDefault "2min";
    };
  })

  # Automatic Boot Assessment natif systemd-boot (boot counting +3 + systemd-bless-boot) si disponible dans nixpkgs
  (lib.optionalAttrs (options.boot.loader.systemd-boot ? bootCounting) {
    boot.loader.systemd-boot.bootCounting = lib.mkIf (cfg.enableHealthChecker && config.boot.loader.systemd-boot.enable) {
      enable = true;
      tries = cfg.maxBootAttempts;
    };
  })
  ]);
}
