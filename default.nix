# Point d'entrée principal du dépôt GitHub.
#
# Utilisation en 1 ligne dans /etc/nixos/configuration.nix (Serveur Steam Deck complet) :
#   imports = [
#     ./hardware-configuration.nix
#     (builtins.fetchTarball "https://github.com/benjamin7490/nixos-steamdeck-cockpit/archive/main.tar.gz")
#   ];
#
# Pour un serveur NixOS classique (sans drivers Steam Deck / Jovian) :
#   imports = [
#     ./hardware-configuration.nix
#     ((builtins.fetchTarball "https://github.com/benjamin7490/nixos-steamdeck-cockpit/archive/main.tar.gz") + "/nixos/cockpit-nixos-module.nix")
#   ];
{ ... }:
{
  imports = [
    ./profiles/steamdeck-server.nix
  ];
}
