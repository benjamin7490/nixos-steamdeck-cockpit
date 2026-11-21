{
  description = "Cockpit-NixOS — Écosystème Cockpit déclaratif pour NixOS + Profil Serveur Steam Deck & Health-Checker MicroOS";

  outputs = { self }: {
    nixosModules = {
      # Profil complet Serveur Steam Deck (Cockpit + Podman + KVM + Files + Health-Checker + Jovian Fan Drivers)
      default = import ./profiles/steamdeck-server.nix;
      steamdeck-server = import ./profiles/steamdeck-server.nix;

      # Module Cockpit-NixOS seul (générique, sans drivers Steam Deck, 100 % pur pour Flakes)
      cockpit-nixos = import ./nixos/cockpit-nixos-module.nix;
    };
  };
}
