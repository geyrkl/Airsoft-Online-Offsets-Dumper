#!/usr/bin/env python3
"""Ground-truth + output sanity checks."""
import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_offsets as eo

if not eo.resolve_paths(None):
    print("Airsoft Online installation not found", file=sys.stderr)
    sys.exit(1)

# --- parse each assembly once, capturing warnings + keeping modules -------- #
modules = []
for path in sorted(eo.MANAGED.glob("*.dll")):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        try:
            modules.append(eo.Module(path))
        except Exception:
            pass
    w = buf.getvalue().strip()
    if w:
        print(f"[warning from {path.name}] {w}")


def check(module_name, type_name, expect_size=None, expect=None):
    mod = next((m for m in modules if m.name == module_name), None)
    if mod is None:
        print(f"SKIP {type_name}: module missing")
        return
    rec = next((r for r in mod.typedefs if r and r.full == type_name), None)
    if rec is None:
        print(f"SKIP {type_name}: not found")
        return
    lay = eo.type_layout(rec)
    got = [(f["name"], f["offset"]) for f in lay["fields"]]
    ok = True
    if expect is not None and got != expect:
        ok = False
    if expect_size is not None and lay["size"] != expect_size:
        ok = False
    print(f"{'OK  ' if ok else 'FAIL'} {type_name}: size={lay['size']} fields={got}")
    if not ok:
        print(f"     expected size={expect_size} fields={expect}")


print("--- engine ground truth ---")
check("UnityEngine.CoreModule.dll", "UnityEngine.Vector4", 16,
      [("x", 0), ("y", 4), ("z", 8), ("w", 12)])
check("UnityEngine.CoreModule.dll", "UnityEngine.Quaternion", 16,
      [("x", 0), ("y", 4), ("z", 8), ("w", 12)])
check("UnityEngine.CoreModule.dll", "UnityEngine.Color", 16,
      [("r", 0), ("g", 4), ("b", 8), ("a", 12)])
check("UnityEngine.CoreModule.dll", "UnityEngine.Rect", 16,
      [("m_XMin", 0), ("m_YMin", 4), ("m_Width", 8), ("m_Height", 12)])
check("UnityEngine.CoreModule.dll", "UnityEngine.Vector2", 8,
      [("x", 0), ("y", 4)])
check("UnityEngine.CoreModule.dll", "UnityEngine.Bounds", 24,
      [("m_Center", 0), ("m_Extents", 12)])

print("--- game output sanity ---")
for name in ("Pixomnia.AirsoftOnline.Game", "Assembly-CSharp",
             "Fusion.Addons.SimpleKCC"):
    p = eo.OUT / f"{name}.json"
    if not p.exists():
        print(f"FAIL missing output {p.name}")
        continue
    data = json.loads(p.read_text(encoding="utf-8"))
    n_types = len(data["types"])
    n_fields = sum(len(t["fields"]) for t in data["types"])
    null_off = sum(1 for t in data["types"] for f in t["fields"]
                   if f["offset"] is None)
    print(f"OK  {p.name}: {n_types} types, {n_fields} instance fields, "
          f"{null_off} null offsets")
    sample = next((t for t in data["types"]
                   if t["fields"] and all(f["offset"] is not None
                                          for f in t["fields"])), None)
    if sample:
        s = sample["fields"][:4]
        print(f"    e.g. {sample['name']} ({sample['kind']}, "
              f"size={sample['instance_size']}): "
              + ", ".join(f"{f['name']}@{f['offset']}" for f in s))
