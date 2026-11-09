"""Package the Claude Code plugin as a .plugin file for the Claude app (Customize > Plugins > Upload plugin).

Usage: python tools/package_plugin.py [plugin folder] [output file]
       (defaults: mod/switch-advisor and dist/switch-advisor.plugin)

A .plugin file is a zip of the plugin folder. Cowork needs it: Cowork runs Claude Code in a virtual machine or in the
cloud, which loads only the plugins in your Claude account. The package leaves out what only development needs: the
tests, the social-media images, and the type files the engine lays into .claude-plugin/types/, with the tsconfig.json
it writes beside them.
"""
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {os.path.join(".claude-plugin", "types"), "node_modules", "__pycache__"}
SKIP_FILES = ("social-card.png", "social-preview.png", "tsconfig.json")


def wanted(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(rel.replace("\\", "/").startswith(d.replace("\\", "/") + "/") for d in SKIP_DIRS) or any(p in SKIP_DIRS for p in parts):
        return False
    name = parts[-1]
    return not (name.endswith((".test.ts", ".test.tsx")) or name in SKIP_FILES or name.startswith("."))


def main():
    src = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "mod", "switch-advisor"))
    manifest = os.path.join(src, ".claude-plugin", "plugin.json")
    with open(manifest, encoding="utf-8") as fh:
        meta = json.load(fh)
    out = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "dist", f"{meta['name']}.plugin"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    files = []
    for dp, dn, fns in os.walk(src):
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), src)
            if wanted(rel) or rel.replace("\\", "/") == ".claude-plugin/plugin.json":
                files.append(rel)
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):
            z.write(os.path.join(src, rel), rel.replace("\\", "/"))
    os.replace(tmp, out)
    print(f"{meta['name']} {meta.get('version', '')}: {len(files)} files -> {out} ({os.path.getsize(out) // 1024} KB)")
    for rel in sorted(files):
        print("  " + rel.replace("\\", "/"))


if __name__ == "__main__":
    main()
