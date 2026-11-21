# ==============================================================================
# Exemple minimal de fichier .nix à copier sur le Steam Deck (/etc/nixos/)
# une fois votre projet poussé sur GitHub.
#
# Il suffit d'importer ce fichier dans /etc/nixos/configuration.nix (ou de
# copier directement le bloc `imports` dans votre configuration.nix existant).
# ==============================================================================
{ ... }:

{
  imports = [
    (builtins.fetchTarball "https://github.com/benjamin7490/nixos-steamdeck-cockpit/archive/main.tar.gz")
  ];

  # Indiquez votre utilisateur NixOS existant sur le serveur (ou laisser [] si les groupes
  # wheel, networkmanager et libvirtd sont déjà définis dans configuration.nix)
  services.steamdeck-server.adminUsers = [ "benjamin" ];

  # Optionnel : passer à true pour une courbe ventilateur serveur plus agressive
  # services.steamdeck-server.enableCustomFanCurve = true;

  # Optionnel : ajuster les horaires de mise à jour automatique / reboot
  # services.cockpit-nixos.autoUpgradeDates = "03:30";
  # services.cockpit-nixos.autoUpgradeRebootWindow = { lower = "03:00"; upper = "05:30"; };
}
