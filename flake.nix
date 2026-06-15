{
  description = "Open Notebook Librarian Pipeline — semantic index for multi-paradigm codebases";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pythonDeps = with pkgs.python312Packages; [
          requests
          fastapi
          uvicorn
          pydantic
        ];

        pipelineRuntime = pkgs.python312.withPackages (_: pythonDeps);

        pipelineScripts = pkgs.runCommand "on-pipeline" { } ''
          mkdir -p $out/bin $out/lib/pipeline
          cp ${./scripts/pipeline/generate_phonebook.py} $out/lib/pipeline/generate_phonebook.py
          cp ${./scripts/pipeline/librarian_server.py} $out/lib/pipeline/librarian_server.py
          cp ${./scripts/pipeline/mcp_librarian_server.py} $out/lib/pipeline/mcp_librarian_server.py
          cp ${./scripts/pipeline/bootstrap_on.py} $out/lib/pipeline/bootstrap_on.py
          cp ${./scripts/pipeline/justfile} $out/lib/pipeline/justfile

          cat > $out/bin/generate-phonebook << 'EOF'
          #!${pkgs.bash}/bin/bash
          exec ${pipelineRuntime}/bin/python3 $out/lib/pipeline/generate_phonebook.py "$@"
          EOF
          chmod +x $out/bin/generate-phonebook

          cat > $out/bin/on-librarian << 'EOF'
          #!${pkgs.bash}/bin/bash
          exec ${pipelineRuntime}/bin/python3 $out/lib/pipeline/librarian_server.py "$@"
          EOF
          chmod +x $out/bin/on-librarian

          cat > $out/bin/on-mcp-bridge << 'EOF'
          #!${pkgs.bash}/bin/bash
          exec ${pipelineRuntime}/bin/python3 $out/lib/pipeline/mcp_librarian_server.py
          EOF
          chmod +x $out/bin/on-mcp-bridge

          cat > $out/bin/on-bootstrap << 'EOF'
          #!${pkgs.bash}/bin/bash
          exec ${pipelineRuntime}/bin/python3 $out/lib/pipeline/bootstrap_on.py "$@"
          EOF
          chmod +x $out/bin/on-bootstrap
        '';

      in {
        packages = {
          pipeline = pipelineScripts;
        };

        apps = {
          pipeline = {
            type = "app";
            program = "${pipelineScripts}/bin/generate-phonebook";
          };

          bootstrap = {
            type = "app";
            program = "${pipelineScripts}/bin/on-bootstrap";
          };

          librarian = {
            type = "app";
            program = "${pipelineScripts}/bin/on-librarian";
          };

          mcp-bridge = {
            type = "app";
            program = "${pipelineScripts}/bin/on-mcp-bridge";
          };
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python312
            python312Packages.requests
            python312Packages.fastapi
            python312Packages.uvicorn
            python312Packages.pydantic
            just
            docker-compose
            jq
            curl
          ];

          shellHook = ''
            echo "Open Notebook Librarian Pipeline"
            echo ""
            echo "Commands:"
            echo "  just status         — Check all services"
            echo "  just bootstrap      — Set up ON transforms + models"
            echo "  just pipeline-fast  — Heuristic-only indexing"
            echo "  just pipeline-full  — Full 3-pass indexing (35B teacher)"
            echo "  just up             — Start ON + librarian"
            echo "  just down           — Stop everything"
          '';
        };
      });
}