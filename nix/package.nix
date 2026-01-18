{
  lib,
  pkgs,
  python3Packages,
}:

let
  ttvlolPlugin = pkgs.fetchurl {
    url = "https://raw.githubusercontent.com/2bc4/streamlink-ttvlol/8a2ebd30dbcbd3caff3f171a1a8c84bc50bc8bd5/twitch.py";
    hash = "sha256-YuShwEQYECXJ9MnH5LHW79r+7A8e13kEfFXVMN4eJLc=";
  };
in

python3Packages.buildPythonApplication {
  pname = "quadlink";
  version = "2025.01.0";

  src = ../.;
  format = "pyproject";

  propagatedBuildInputs =
    (with python3Packages; [
      aiohttp
      httpx
      pydantic
      pydantic-settings
      ruamel-yaml
      structlog
    ])
    ++ [ pkgs.streamlink ];

  nativeBuildInputs = with python3Packages; [
    setuptools
    wheel
  ];

  postInstall = ''
    mkdir -p $out/${python3Packages.python.sitePackages}/quadlink/plugins
    cp ${ttvlolPlugin} $out/${python3Packages.python.sitePackages}/quadlink/plugins/twitch.py
  '';

  doCheck = false;

  meta = with lib; {
    description = "Companion to QuadStream for Apple tvOS";
    homepage = "https://github.com/etedor/quadlink";
    license = licenses.mit;
    maintainers = [ ];
    platforms = platforms.linux ++ platforms.darwin;
  };
}
