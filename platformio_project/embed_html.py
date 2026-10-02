# Pre-build step: bakes ../remote.html into include/remote_html.h so the
# firmware can serve the page at "/".
Import("env")  # noqa: F821 — provided by PlatformIO
from pathlib import Path

DELIM = "rawhtml"

project = Path(env["PROJECT_DIR"])  # noqa: F821
source = project.parent / "remote.html"
target = project / "include" / "remote_html.h"

html = source.read_text(encoding="utf-8")
if f"){DELIM}\"" in html:
    raise SystemExit(f"remote.html contains the raw string delimiter ){DELIM}\"")

header = (
    "// Generated from ../remote.html by embed_html.py — do not edit\n"
    "#pragma once\n"
    "#include <pgmspace.h>\n\n"
    f"static const char REMOTE_HTML[] PROGMEM = R\"{DELIM}({html}){DELIM}\";\n"
)

target.parent.mkdir(exist_ok=True)
if not target.exists() or target.read_text(encoding="utf-8") != header:
    target.write_text(header, encoding="utf-8")
    print(f"embed_html: wrote {target.name} ({len(html)} bytes of HTML)")
