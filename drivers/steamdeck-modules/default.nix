{
  lib,
  stdenv,
  kernel,
  kernelModuleMakeFlags ? [ ],
}:

stdenv.mkDerivation {
  pname = "steamdeck-ec-modules";
  version = "7.3.1-valve3-${kernel.version}";

  src = ./.;

  nativeBuildInputs = kernel.moduleBuildDependencies;

  makeFlags = kernelModuleMakeFlags ++ [
    "KDIR=${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
    "INSTALL_MOD_PATH=${placeholder "out"}"
    "INSTALL_MOD_DIR=extra"
    "INSTALL_MOD_STRIP=1"
    "DEPMOD=true"
  ];

  enableParallelBuilding = true;
  buildTargets = [ "modules" ];
  installTargets = [ "modules_install" ];

  meta = with lib; {
    description = "Out-of-tree Steam Deck EC kernel modules (steamdeck, steamdeck-hwmon, leds-steamdeck)";
    license = licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
  };
}
