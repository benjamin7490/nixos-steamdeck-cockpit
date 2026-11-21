# Exemple complet de /etc/nixos/configuration.nix activant l'écosystème
# Cockpit Déclaratif pour NixOS (cockpit-machines, cockpit-files, cockpit-podman,
# interception réseau/pare-feu/paquets -> /etc/nixos/cockpit-managed.nix).

{ config, pkgs, lib, ... }:

{
  imports = [
    ./hardware-configuration.nix
    ./cockpit-nixos-module.nix
  ] ++ lib.optional (builtins.pathExists /etc/nixos/cockpit-managed.nix) /etc/nixos/cockpit-managed.nix;

  # Bootloader (adapter si GRUB au lieu de systemd-boot)
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # Réseau de base
  networking.hostName = "nixos-cockpit-srv";
  networking.networkmanager.enable = true;

  # Localisation
  time.timeZone = "Europe/Paris";
  i18n.defaultLocale = "fr_FR.UTF-8";
  console.keyMap = "fr";

  # Accès SSH sécurisé
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "no";
      PasswordAuthentication = true; # Passer à false après ajout de clés SSH
    };
  };

  # Utilisateur administrateur Cockpit (doit appartenir à wheel, libvirtd, networkmanager)
  users.users.admin = {
    isNormalUser = true;
    description = "Administrateur Serveur NixOS";
    extraGroups = [ "wheel" "networkmanager" "libvirtd" ];
  };

  # Activation du module Cockpit NixOS Déclaratif
  services.cockpit-nixos = {
    enable = true;
    port = 9090;
    # Ajouter l'IP LAN ou le domaine reverse-proxy de votre serveur
    allowedOrigins = [
      "https://localhost:9090"
      "https://nixos-cockpit-srv.local:9090"
      "https://192.168.1.50:9090"
    ];
    enableMachines = true;
    enablePodman = true;
    enableFiles = true;
    enableNetworkInterception = true;
    enableFirewallBridge = true;
    enableAutoUpgrade = true;
    autoUpgradeDates = "03:30";
    autoUpgradeRebootWindow = {
      lower = "03:00";
      upper = "05:30";
    };
    enableHealthChecker = true;
    maxBootAttempts = 3;
  };

  # Découverte mDNS (.local) pratique en réseau local
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    settings.publish = {
      disable-publishing = false;
      publish-addresses = true;
      publish-workstation = true;
    };
  };

  nixpkgs.config.allowUnfree = true;
  system.stateVersion = "24.11"; # Conserver la valeur générée lors de votre installation initiale
}
