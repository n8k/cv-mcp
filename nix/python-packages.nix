# FastMCP 4.1.3 and the dependencies nixpkgs does not carry at the versions it needs.
#
# Added beside the python package set as wheel derivations (no overlay, no override), so
# every other package stays a binary-cache hit and only these small installs are built.
# Checked against nixos-26.05 (fastmcp 3.2.3, mcp 1.26.0 there). Remove an entry once
# nixpkgs has a new enough version.
#
# Relaxed pins: nixpkgs is a little behind on certifi (CA data only), idna and aiofile;
# the code paths cv-mcp uses do not depend on the newer releases. The runtime deps check
# still verifies everything else.
{ ps, fetchurl }:
let
  wheel =
    {
      pname,
      version,
      url,
      hash,
      dependencies,
      pythonRelaxDeps ? [ ],
    }:
    ps.buildPythonPackage {
      inherit
        pname
        version
        dependencies
        pythonRelaxDeps
        ;
      format = "wheel";
      src = fetchurl { inherit url hash; };
      # Wheels ship no tests; the app's own tests cover the paths cv-mcp uses.
      doCheck = false;
    };

  self = {
    # nixpkgs' truststore lists its test tools (aiohttp, httpx, pyopenssl, requests, trustme)
    # as runtime dependencies, which pulled aiohttp and its brotli speedup into the image.
    # Upstream truststore has no dependencies at all.
    truststore = wheel {
      pname = "truststore";
      version = "0.10.4";
      url = "https://files.pythonhosted.org/packages/19/97/56608b2249fe206a67cd573bc93cd9896e1efb9e98bce9c163bcdc704b88/truststore-0.10.4-py3-none-any.whl";
      hash = "sha256-ra6uzxy7X03jsZWbQtQfb6tXsrFmattZ6JywtTNh2YE=";
      dependencies = [ ];
    };

    # nixpkgs has 2.8.0 (CVE-2026-114605, fixed in 2.9.0). Pulled in by email-validator,
    # which pydantic[email] needs; a hard requirement of fastmcp-slim.
    dnspython = wheel {
      pname = "dnspython";
      version = "2.9.0";
      url = "https://files.pythonhosted.org/packages/10/02/cdcc9b7c051786a103c3b09e1003a82fa0c66bcb91ffbdabcfbf7b4163b9/dnspython-2.9.0-py3-none-any.whl";
      hash = "sha256-mkrtuDPDwbSSFNBNRNMDKrepE198HSmlSbT/eP2C/ak=";
      dependencies = [ ];
    };

    # Same version as nixpkgs, built against the dnspython above instead of nixpkgs'.
    email-validator = wheel {
      pname = "email-validator";
      version = "2.3.0";
      url = "https://files.pythonhosted.org/packages/de/15/545e2b6cf2e3be84bc1ed85613edd75b8aea69807a71c26f4ca6a9258e82/email_validator-2.3.0-py3-none-any.whl";
      hash = "sha256-gPE/YjQT5rGXrnO7EL9OsJCPr1Ca2DYsXt6wvn/UULQ=";
      dependencies = [
        self.dnspython
        ps.idna
      ];
    };

    mcp-types = wheel {
      pname = "mcp-types";
      version = "2.3.0";
      url = "https://files.pythonhosted.org/packages/ce/48/32583066b155bb5d4507e7887dc88ee23118cc4de8e2a12870c49719e2a8/mcp_types-2.3.0-py3-none-any.whl";
      hash = "sha256-lo79va7fqwatrkDTeKNDlfEJDFkh1L48nN4oOq922R0=";
      dependencies = with ps; [
        pydantic
        typing-extensions
      ];
    };

    httpcore2 = wheel {
      pname = "httpcore2";
      version = "2.16.0";
      url = "https://files.pythonhosted.org/packages/af/c2/ce417c397afa5181f0e3c6ff01a4d01639414e1adef08730d97571297151/httpcore2-2.16.0-py3-none-any.whl";
      hash = "sha256-XENYMZ8mAtmErZMYLJ/uUWDyLp2nur3ahwpefE4tw08=";
      dependencies = with ps; [
        h11
        self.truststore
      ];
    };

    httpx2 = wheel {
      pname = "httpx2";
      # 2.12.0+ fixes CVE-2026-84382; mcp needs >= 2.10.
      version = "2.16.0";
      url = "https://files.pythonhosted.org/packages/7b/3d/2e4bc4fd1ef9770871669adb348d612a6e4786b303a23f9935b81c48dd60/httpx2-2.16.0-py3-none-any.whl";
      hash = "sha256-/SxdeQno2G/pLVMCDDEMoOTkMbg4wgfFdUubvwMI1Jo=";
      dependencies = with ps; [
        anyio
        self.httpcore2
        idna
        self.truststore
      ];
      pythonRelaxDeps = [ "idna" ];
    };

    mcp = wheel {
      pname = "mcp";
      version = "2.3.0";
      url = "https://files.pythonhosted.org/packages/20/d7/4a2878f128df729db3ca04e71bfce26405cc2e0c9ebe19c592feeb4c61b9/mcp-2.3.0-py3-none-any.whl";
      hash = "sha256-3QxEwInRZFPorjGjh3oAVNeiMUyqqB9eBUG5sXNLI3c=";
      dependencies =
        with ps;
        [
          anyio
          self.httpx2
          jsonschema
          self.mcp-types
          opentelemetry-api
          pydantic
          pyjwt
          python-multipart
          sse-starlette
          starlette
          typing-extensions
          typing-inspection
          uvicorn
        ]
        ++ pyjwt.optional-dependencies.crypto;
    };

    py-key-value-aio = wheel {
      pname = "py-key-value-aio";
      version = "0.4.6";
      url = "https://files.pythonhosted.org/packages/9a/46/12871550fb0613d149144bb9d41f22680d5f46717bc45a76eb5798c20c3f/py_key_value_aio-0.4.6-py3-none-any.whl";
      hash = "sha256-ggswx5WfxzjqPQAWDNsQzSWPUeOHN7Kt1TDe/uod7Xc=";
      # Base plus the filetree, keyring and memory extras fastmcp asks for.
      dependencies = with ps; [
        beartype
        typing-extensions
        aiofile
        anyio
        keyring
        cachetools
      ];
      pythonRelaxDeps = [ "aiofile" ];
    };

    openapi-pydantic = wheel {
      pname = "openapi-pydantic";
      version = "0.6.0";
      url = "https://files.pythonhosted.org/packages/31/57/9d3eb823a03c33e9d4c810e6a3276e3a6dc820a7fdc0961db2c04e613817/openapi_pydantic-0.6.0-py3-none-any.whl";
      hash = "sha256-i9bVSKJQ2jfblvRhvNUrNGjog0rX1tsUsAprLrvRY6M=";
      dependencies = [ ps.pydantic ];
    };

    uncalled-for = wheel {
      pname = "uncalled-for";
      version = "0.4.1";
      url = "https://files.pythonhosted.org/packages/bf/a6/ba763e3812e572de7291676c3a41adfac1ef1209a6acda6bae055970f127/uncalled_for-0.4.1-py3-none-any.whl";
      hash = "sha256-thGziHnZ/lCIKX57so8t3RLS1z2tkz2T2hB/tj60BY4=";
      dependencies = [ ];
    };

    fastmcp-slim = wheel {
      pname = "fastmcp-slim";
      version = "4.1.3";
      url = "https://files.pythonhosted.org/packages/4e/ff/a7feaeb130a2003700789ce32e933b4c2ae00fc7c264642e57ab12e4684f/fastmcp_slim-4.1.3-py3-none-any.whl";
      hash = "sha256-ga+GmTMcM9Q4yvhbaUfzbfbi8EEY5vMYygA3T9mv6po=";
      # Base plus the client and server extras (what `fastmcp` itself installs), except
      # authlib: only the OAuth proxy (fastmcp.server.auth, loaded lazily) imports it, and
      # cv-mcp has no OAuth. It carried CVE-2026-96760 (critical) and two highs.
      dependencies = with ps; [
        self.mcp-types
        platformdirs
        pydantic-settings
        pydantic
        python-dotenv
        rich
        typing-extensions
        exceptiongroup
        self.httpx2
        self.mcp
        opentelemetry-api
        self.py-key-value-aio
        starlette
        certifi
        cyclopts
        griffelib
        joserfc
        jsonref
        jsonschema-path
        self.openapi-pydantic
        packaging
        pyperclip
        python-multipart
        pyyaml
        self.uncalled-for
        uvicorn
        watchfiles
        websockets
        self.email-validator # pydantic[email], with the dnspython above
      ];
      pythonRelaxDeps = [ "certifi" ];
    };

    fastmcp = wheel {
      pname = "fastmcp";
      version = "4.1.3";
      url = "https://files.pythonhosted.org/packages/97/7f/27c501d98b5681ca4f6c21a7d741b3e732e962c5e4c3b4db6dd1ffdf4fd8/fastmcp-4.1.3-py3-none-any.whl";
      hash = "sha256-uYPYrMRKsRQ+DIb9bahD8lBD7grrZKuYOzbFJ70dM+k=";
      dependencies = [ self.fastmcp-slim ];
    };
  };
in
self
