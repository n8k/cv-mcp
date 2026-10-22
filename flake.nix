{
  description = "cv-mcp: a CV served as a read-only MCP server";

  # Same channel as the Pi's NixOS config, so the Python set is a binary-cache hit there.
  # After `nix flake update`, check that only the wheels in nix/python-packages.nix and the
  # app itself are built:  nix build --dry-run .#cv-mcp .#image
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      # aarch64: the Pi (CI and the testing tier). x86_64: Azure Container Apps (production).
      systems = [
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Only what the app (and, for checks, its tests) reads, so doc, CI or test edits do
      # not change the image.
      appFiles = [
        ./server.py
        ./cv.toml
      ];
      toSource =
        files:
        nixpkgs.lib.fileset.toSource {
          root = ./.;
          fileset = nixpkgs.lib.fileset.unions files;
        };
      src = toSource appFiles;
      testSrc = toSource (
        appFiles
        ++ [
          ./pyproject.toml
          ./examples
          ./tests
        ]
      );

      pythonFor =
        pkgs:
        let
          py = pkgs.python3;
          extra = import ./nix/python-packages.nix {
            ps = py.pkgs;
            inherit (pkgs) fetchurl;
          };
        in
        {
          runtime = py.withPackages (ps: [
            extra.fastmcp
            ps.pydantic
            ps.starlette
            ps.uvicorn
          ]);
          dev = py.withPackages (ps: [
            extra.fastmcp
            ps.pydantic
            ps.starlette
            ps.uvicorn
            ps.httpx # starlette's TestClient
            ps.pytest
          ]);
        };
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          python = (pythonFor pkgs).runtime;

          cv-mcp = pkgs.runCommand "cv-mcp" { meta.mainProgram = "cv-mcp"; } ''
            mkdir -p $out/share/cv-mcp $out/bin
            # server.py reads cv.toml from its own directory unless CV_PATH is set.
            cp ${src}/server.py ${src}/cv.toml $out/share/cv-mcp/
            cat > $out/bin/cv-mcp <<EOF
            #!${pkgs.runtimeShell}
            exec ${python}/bin/python $out/share/cv-mcp/server.py "\$@"
            EOF
            # Container health check: the image has no curl.
            cat > $out/bin/cv-mcp-health <<EOF
            #!${pkgs.runtimeShell}
            exec ${python}/bin/python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/health" % os.environ.get("PORT", "8000"), timeout=5)'
            EOF
            chmod +x $out/bin/*
          '';
        in
        {
          inherit cv-mcp;
          default = cv-mcp;

          # A script that streams the image tar to stdout (run it, do not docker load the path).
          # Built for the system it is evaluated for: .#packages.x86_64-linux.image is the
          # production image, .#image on the Pi is the aarch64 testing image.
          image = pkgs.dockerTools.streamLayeredImage {
            name = "cv-mcp";
            tag = "latest"; # the pipeline pushes it under the commit SHA
            contents = [ cv-mcp ];
            extraCommands = "mkdir -m 1777 tmp";
            config = {
              Cmd = [ "${cv-mcp}/bin/cv-mcp" ];
              User = "65534:65534";
              Env = [
                "HOST=0.0.0.0"
                "PORT=8000"
                "HOME=/tmp"
              ];
              ExposedPorts."8000/tcp" = { };
              Healthcheck = {
                Test = [
                  "CMD"
                  "${cv-mcp}/bin/cv-mcp-health"
                ];
                Interval = 30000000000;
                Timeout = 10000000000;
                Retries = 3;
              };
            };
          };
        }
      );

      # The test suite as a derivation: CI runs it with `nix flake check`, so the Python
      # closure lands in the host store once instead of being resolved on every run.
      checks = forAllSystems (pkgs: {
        tests = pkgs.runCommand "cv-mcp-tests" { nativeBuildInputs = [ (pythonFor pkgs).dev ]; } ''
          cp -r ${testSrc}/. . && chmod -R u+w .
          HOME=$TMPDIR pytest -q -p no:cacheprovider
          touch $out
        '';
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.cv-mcp;
      });

      devShells = forAllSystems (
        pkgs:
        let
          dev = (pythonFor pkgs).dev;
        in
        {
          # `nix develop`: run the server and the tests locally.
          default = pkgs.mkShell {
            packages = [
              dev
              pkgs.ruff
              pkgs.nixfmt
            ];
          };

          # What CI runs: linters, the test runner, and the image tools (no Docker socket).
          ci = pkgs.mkShell {
            packages = with pkgs; [
              dev
              ruff
              nixfmt
              skopeo
              trivy
              curl
              jq
            ];
          };

          # Production promotion only (azure-cli is large; kept out of the ci shell).
          deploy = pkgs.mkShell {
            packages = with pkgs; [
              azure-cli
              skopeo
              curl
            ];
          };
        }
      );

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
