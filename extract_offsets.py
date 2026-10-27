#!/usr/bin/env python3
"""Extract managed metadata offsets from the Airsoft Online (Unity / Mono) assemblies.

Usage:

    python extract_offsets.py                 # all assemblies
    python extract_offsets.py --game-only     # skip engine/framework assemblies
    python extract_offsets.py <gamedir>       # explicit path to the game folder

The script locates the Airsoft Online installation automatically — first the
folder it is placed in, then every Steam library ("Airsoft Online Demo" or
"Airsoft Online Beta") — then it automatically creates an "offsets" folder
inside the game directory and writes one <AssemblyName>.json per assembly
plus index.json there.

Requires: pip install dnfile
"""
from __future__ import annotations

import json
import re
import struct
import sys
import traceback
from pathlib import Path

import dnfile

HERE = Path(__file__).resolve().parent
GAME_DIRS = ("Airsoft Online Demo", "Airsoft Online Beta")
DEFAULT_STEAM = Path(r"C:\Program Files (x86)\Steam")

# resolved by resolve_paths():
GAME_ROOT: Path | None = None      # the game installation folder
MANAGED: Path | None = None        # <game>/AirsoftOnline_Data/Managed
OUT: Path | None = None            # offsets output folder (created if missing)

# Target layout model: 64-bit Mono, default packing.
PTR = 8            # managed reference / pointer size (x64 Mono)
DEFAULT_PACK = 8

# ECMA-335 II.23.2.2 element types (values as emitted by the Mono builds here:
# 0x1C = object, 0x1D = SZARRAY, 0x1E = MVAR, 0x1F/0x20 = CMOD_REQD/CMOD_OPT)
PRIM = {
    0x01: "void", 0x02: "bool", 0x03: "char",
    0x04: "sbyte", 0x05: "byte", 0x06: "short", 0x07: "ushort",
    0x08: "int", 0x09: "uint", 0x0A: "long", 0x0B: "ulong",
    0x0C: "float", 0x0D: "double", 0x0E: "string",
    0x18: "IntPtr", 0x19: "UIntPtr", 0x1C: "object",
}
PRIM_SIZE = {
    "void": 0, "bool": 1, "char": 2, "sbyte": 1, "byte": 1,
    "short": 2, "ushort": 2, "int": 4, "uint": 4,
    "long": 8, "ulong": 8, "float": 4, "double": 8,
    "string": PTR, "IntPtr": PTR, "UIntPtr": PTR, "object": PTR,
}

# Assemblies that are engine/framework code, not the game itself.
SKIP_PREFIX = ("UnityEngine", "System", "Microsoft.", "Unity.",
               "Autodesk.", "Sirenix.", "Newtonsoft.")
SKIP_EXACT = {"mscorlib.dll", "netstandard.dll", "Mono.Security.dll"}


def flag(flags, name: str) -> bool:
    return bool(getattr(flags, name, False))


def rnd(v: int, a: int) -> int:
    if a <= 1:
        return v
    return (v + a - 1) // a * a


def istr(obj) -> str:
    v = getattr(obj, "value", None)
    return v if isinstance(v, str) else str(obj)


def is_game_assembly(name: str) -> bool:
    if name in SKIP_EXACT:
        return False
    return not any(name.startswith(p) for p in SKIP_PREFIX)


def _steam_library_commons() -> list[Path]:
    """steamapps/common folders of every Steam library on this machine."""
    out: list[Path] = []
    vdf = DEFAULT_STEAM / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            common = Path(m.group(1).replace("\\\\", "\\")) / "steamapps" / "common"
            if common.is_dir() and common not in out:
                out.append(common)
    std = DEFAULT_STEAM / "steamapps" / "common"
    if std.is_dir() and std not in out:
        out.append(std)
    return out


def resolve_paths(explicit: str | None = None) -> bool:
    """Find the game install and set GAME_ROOT / MANAGED / OUT.

    Search order: explicit argument -> the folder the script lives in ->
    every Steam library ("Airsoft Online Demo", then "Airsoft Online Beta").
    The output "offsets" folder is created automatically.
    """
    global GAME_ROOT, MANAGED, OUT
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit).resolve())
    cands.extend((HERE.parent, HERE))
    for common in _steam_library_commons():
        cands.extend(common / name for name in GAME_DIRS)
    for p in cands:
        if (p / "AirsoftOnline_Data" / "Managed").is_dir():
            GAME_ROOT = p
            MANAGED = p / "AirsoftOnline_Data" / "Managed"
            OUT = HERE if (HERE.name == "offsets" and HERE.parent == p) else p / "offsets"
            OUT.mkdir(parents=True, exist_ok=True)
            return True
    return False


def field_token(row: int) -> str:
    return f"0x{0x04000000 | row:08x}"


def method_token(row: int) -> str:
    return f"0x{0x06000000 | row:08x}"


# --------------------------------------------------------------------------- #
#  constant (literal) values — ECMA-335 II.22.9 Constant table
# --------------------------------------------------------------------------- #
def _clean_str(s: str) -> str:
    """Replace lone surrogates so the result is JSON/UTF-8 safe."""
    try:
        return s.encode("utf-8", "replace").decode("utf-8")
    except UnicodeError:
        return s


def decode_const(t: int, raw: bytes):
    """Decode a Constant heap value by its ELEMENT_TYPE byte."""
    try:
        if t == 0x02:                                   # BOOLEAN
            return bool(raw[0]) if raw else False
        if t == 0x03:                                   # CHAR
            if not raw:
                return ""
            c = chr(int.from_bytes(raw[:2], "little"))
            return "" if 0xD800 <= ord(c) <= 0xDFFF else c
        if t == 0x0E:                                   # STRING
            return _clean_str(raw.decode("utf-16-le", "replace"))
        if t in (0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B):
            n = {0x04: 1, 0x05: 1, 0x06: 2, 0x07: 2,
                 0x08: 4, 0x09: 4, 0x0A: 8, 0x0B: 8}[t]
            return int.from_bytes(raw[:n], "little",
                                  signed=t in (0x04, 0x06, 0x08, 0x0A))
        if t == 0x0C:                                   # R4
            return struct.unpack("<f", raw[:4])[0]
        if t == 0x0D:                                   # R8
            return struct.unpack("<d", raw[:8])[0]
        if t in (0x12, 0x1C):                           # CLASS -> null ref
            return None
    except Exception:
        return raw.hex()
    return raw.hex() if raw else None


# --------------------------------------------------------------------------- #
#  signature decoding (ECMA-335 II.23.2.1)
# --------------------------------------------------------------------------- #
class SigReader:
    def __init__(self, data: bytes, module: "Module"):
        self.d = data
        self.p = 0
        self.m = module

    def u8(self) -> int:
        if self.p >= len(self.d):
            return 0
        b = self.d[self.p]
        self.p += 1
        return b

    def cuint(self) -> int:
        b = self.u8()
        if b & 0x80 == 0:
            return b
        if b & 0xC0 == 0x80:
            return ((b & 0x3F) << 8) | self.u8()
        return ((b & 0x1F) << 24) | (self.u8() << 16) | (self.u8() << 8) | self.u8()

    def read_type(self) -> dict:
        b = self.u8()
        if b in PRIM:
            return {"k": "prim", "name": PRIM[b]}
        if b == 0x0F:                                   # PTR
            return {"k": "ptr", "elem": self.read_type()}
        if b == 0x10:                                   # BYREF
            return {"k": "byref", "elem": self.read_type()}
        if b in (0x11, 0x12):                           # VALUETYPE / CLASS
            return {"k": "valuetype" if b == 0x11 else "class",
                    "token": self.cuint(), "module": self.m}
        if b == 0x13:                                   # VAR  (!n)
            return {"k": "var", "index": self.cuint()}
        if b == 0x1E:                                   # MVAR (!!n)
            return {"k": "mvar", "index": self.cuint()}
        if b == 0x14:                                   # ARRAY
            elem = self.read_type()
            rank = self.cuint()
            for _ in range(rank * 2):                   # sizes + lower bounds
                self.cuint()
            return {"k": "array", "elem": elem}
        if b == 0x15:                                   # GENERICINST
            gb = self.u8()
            tok = self.cuint()
            n = self.cuint()
            args = [self.read_type() for _ in range(n)]
            return {"k": "ginst",
                    "kind": "valuetype" if gb == 0x11 else "class",
                    "token": tok, "module": self.m, "args": args}
        if b == 0x1B:                                   # FNPTR (method sig follows)
            return {"k": "fnptr"}
        if b == 0x1D:                                   # SZARRAY
            return {"k": "szarray", "elem": self.read_type()}
        if b in (0x1F, 0x20):                           # CMOD_REQD / CMOD_OPT
            self.cuint()
            return self.read_type()
        if b in (0x41, 0x45):                           # SENTINEL / PINNED
            return self.read_type()
        if b == 0x16:                                   # TYPEDBYREF
            return {"k": "prim", "name": "typedbyref"}
        return {"k": "unknown", "et": b}

    def read_field(self) -> dict | None:
        if not self.d:
            return None
        self.p = 1 if self.d[0] == 0x06 else 0          # FIELD prolog
        return self.read_type()


# --------------------------------------------------------------------------- #
#  metadata model
# --------------------------------------------------------------------------- #
class TypeRec:
    __slots__ = ("module", "idx", "name", "full", "base_sig", "fields",
                 "methods", "pack", "class_size", "field_layout",
                 "is_struct", "is_enum", "is_iface", "explicit",
                 "_layout_cache")

    def __init__(self, module, idx, name, flags):
        self.module = module
        self.idx = idx                    # 1-based TypeDef row
        self.name = name
        self.full = name
        self.base_sig = None              # sig dict or {"k":"class","name":...}
        self.fields = []                  # {row, name, sig, static, literal}
        self.methods = []                 # {row, name, rva}
        self.pack = 0
        self.class_size = 0
        self.field_layout = {}            # field row -> recorded offset
        self.is_struct = False
        self.is_enum = False
        self.is_iface = flag(flags, "tdInterface")
        self.explicit = flag(flags, "tdExplicitLayout")
        self._layout_cache = {}


REGISTRY: dict[str, list[TypeRec]] = {}


def registry_add(rec: TypeRec) -> None:
    REGISTRY.setdefault(rec.full, []).append(rec)


def registry_lookup(full: str, asm_hint: str | None = None) -> TypeRec | None:
    cands = REGISTRY.get(full)
    if not cands:
        return None
    if asm_hint:
        for c in cands:
            if c.module.name == asm_hint:
                return c
    return cands[0]


class Module:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.pe = dnfile.dnPE(str(path))
        self.md = self.pe.net.mdtables
        # index 0 is a sentinel so typedefs[rid] == the row with that 1-based rid
        self.typedefs: list[TypeRec | None] = [None]
        self._typeref_cache: dict[int, tuple[str, str | None]] = {}
        self._read_constants()
        self._read_typedefs()
        self._read_nesting()
        self._read_layouts()

    # ---- typedefs, fields, methods ---------------------------------------- #
    def _read_typedefs(self):
        for i, row in enumerate(self.md.TypeDef.rows, start=1):
            name = istr(row.TypeName)
            if name == "<Module>":
                self.typedefs.append(None)
                continue
            rec = TypeRec(self, i, name, row.Flags)
            ns = istr(row.TypeNamespace)
            rec.full = f"{ns}.{name}" if ns else name

            ext = row.Extends
            erow = ext.row if ext is not None else None
            if erow is not None:
                cls = type(erow).__name__
                if cls == "TypeRefRow":
                    ename, _asm = self.typeref_info(erow)
                elif cls == "TypeDefRow":
                    ens = istr(getattr(erow, "TypeNamespace", "") or "")
                    enm = istr(erow.TypeName)
                    ename = f"{ens}.{enm}" if ens else enm
                else:                                       # TypeSpec (generic base)
                    ename = None
                if ename is not None:
                    rec.base_sig = {"k": "class", "name": ename}
                    rec.is_struct = ename == "System.ValueType"
                    rec.is_enum = ename == "System.Enum"

            for fi in row.FieldList:
                frow = fi.row
                fflags = frow.Flags
                sig = None
                if frow.Signature is not None:
                    sig = SigReader(frow.Signature.value, self).read_field()
                fd = {
                    "row": fi.row_index, "name": istr(frow.Name), "sig": sig,
                    "static": flag(fflags, "fdStatic"),
                    "literal": flag(fflags, "fdLiteral"),
                }
                cinfo = self._const_by_field.get(fi.row_index)
                if cinfo is not None:
                    fd["cvalue"] = decode_const(*cinfo)
                rva = self._fieldrva.get(fi.row_index)
                if rva:
                    fd["rva"] = rva
                    if sig_name(sig) == "string":
                        fd["sdata"] = self._string_data(rva)
                rec.fields.append(fd)
            for mi in row.MethodList:
                mrow = mi.row
                rec.methods.append({
                    "row": mi.row_index, "name": istr(mrow.Name),
                    "rva": int(mrow.Rva or 0),
                })
            self.typedefs.append(rec)

    def _read_nesting(self):
        if self.md.NestedClass:
            for row in self.md.NestedClass.rows:
                n = row.NestedClass.row_index
                e = row.EnclosingClass.row_index
                if 0 < n < len(self.typedefs) and 0 < e < len(self.typedefs):
                    child, parent = self.typedefs[n], self.typedefs[e]
                    if child is not None and parent is not None:
                        child.full = f"{parent.full}+{child.name}"
        for rec in self.typedefs:
            if rec is not None:
                registry_add(rec)

    def _read_layouts(self):
        if self.md.ClassLayout:
            for row in self.md.ClassLayout.rows:
                p = row.Parent.row_index if row.Parent else 0
                if 0 < p < len(self.typedefs) and self.typedefs[p] is not None:
                    rec = self.typedefs[p]
                    rec.pack = int(row.PackingSize or 0)
                    rec.class_size = int(row.ClassSize or 0)
        fl = {}
        if self.md.FieldLayout:
            for row in self.md.FieldLayout.rows:
                if row.Field is not None:
                    fl[row.Field.row_index] = int(row.Offset or 0)
        for rec in self.typedefs:
            if rec is None:
                continue
            for f in rec.fields:
                if f["row"] in fl:
                    rec.field_layout[f["row"]] = fl[f["row"]]

    def _string_data(self, rva: int) -> str | None:
        """Decode a string initializer: u32 char count + UTF-16 data."""
        try:
            head = bytes(self.pe.get_data(rva, 4))
            n = int.from_bytes(head, "little")
            if n > 8192:
                return None
            return _clean_str(bytes(self.pe.get_data(rva + 4, n * 2))
                              .decode("utf-16-le", "replace"))
        except Exception:
            return None

    def _read_constants(self):
        """Map Field rids -> (const type byte, raw value) and FieldRva data."""
        self._const_by_field: dict[int, tuple[int, bytes]] = {}
        if self.md.Constant:
            for row in self.md.Constant.rows:
                p = row.Parent
                r = p.row if p is not None else None
                if r is None or type(r).__name__ != "FieldRow":
                    continue
                t = getattr(row.Type, "value", row.Type)
                self._const_by_field[p.row_index] = (int(t),
                                                     bytes(row.Value.value or b""))
        self._fieldrva: dict[int, int] = {}
        if self.md.FieldRva:
            for row in self.md.FieldRva.rows:
                fidx = row.Field
                if fidx is None or fidx.row is None:
                    continue
                self._fieldrva[fidx.row_index] = int(row.Rva or 0)

    # ---- typerefs --------------------------------------------------------- #
    def typeref_info(self, row) -> tuple[str, str | None]:
        key = id(row)
        if key in self._typeref_cache:
            return self._typeref_cache[key]
        ens = istr(row.TypeNamespace or "")
        nm = istr(row.TypeName)
        asm = None
        scope = row.ResolutionScope
        srow = scope.row if scope is not None else None
        if srow is not None:
            cls = type(srow).__name__
            if cls == "AssemblyRefRow":
                asm = istr(srow.Name)
            elif cls == "TypeRefRow":
                parent, pasm = self.typeref_info(srow)
                out = (f"{parent}+{nm}", pasm)
                self._typeref_cache[key] = out
                return out
        out = (f"{ens}.{nm}" if ens else nm, asm)
        self._typeref_cache[key] = out
        return out

    def resolve_token(self, tok: int):
        """TypeRec or sig dict for a TypeDefOrRef coded token, else None."""
        tag, rid = tok & 3, tok >> 2
        if tag == 0:
            if 0 < rid < len(self.typedefs):
                return self.typedefs[rid]
            return None
        if tag == 1:
            if self.md.TypeRef and 0 < rid <= len(self.md.TypeRef.rows):
                full, asm = self.typeref_info(self.md.TypeRef.rows[rid - 1])
                return registry_lookup(full, asm)
            return None
        if tag == 2:
            if self.md.TypeSpec and 0 < rid <= len(self.md.TypeSpec.rows):
                srow = self.md.TypeSpec.rows[rid - 1]
                if srow.Signature is not None:
                    return SigReader(srow.Signature.value, self).read_type()
            return None
        return None


# --------------------------------------------------------------------------- #
#  layout engine (metadata-order sequential / explicit, x64, pack 8)
# --------------------------------------------------------------------------- #
def sig_name(sig: dict | None) -> str:
    if not sig:
        return "?"
    k = sig["k"]
    if k == "prim":
        return sig["name"]
    if k == "ptr":
        return sig_name(sig.get("elem")) + "*"
    if k == "byref":
        return sig_name(sig.get("elem")) + "&"
    if k == "szarray":
        return sig_name(sig.get("elem")) + "[]"
    if k == "array":
        return sig_name(sig.get("elem")) + "[*]"
    if k == "fnptr":
        return "fnptr"
    if k == "var":
        return f"!{sig['index']}"
    if k == "mvar":
        return f"!!{sig['index']}"
    if k in ("class", "valuetype"):
        if "name" in sig:
            return sig["name"]
        return _token_name(sig["module"], sig["token"])
    if k == "ginst":
        base = _token_name(sig["module"], sig["token"])
        return base + "<" + ", ".join(sig_name(a) for a in sig["args"]) + ">"
    if k == "unknown":
        return f"et0x{sig.get('et', 0):02x}"
    return str(k)


def _token_name(module: Module, tok: int) -> str:
    tag, rid = tok & 3, tok >> 2
    if tag == 0:
        if 0 < rid < len(module.typedefs) and module.typedefs[rid] is not None:
            return module.typedefs[rid].full
        return f"TypeDef#{rid}"
    if tag == 1 and module.md.TypeRef and 0 < rid <= len(module.md.TypeRef.rows):
        return module.typeref_info(module.md.TypeRef.rows[rid - 1])[0]
    if tag == 2 and module.md.TypeSpec and 0 < rid <= len(module.md.TypeSpec.rows):
        srow = module.md.TypeSpec.rows[rid - 1]
        if srow.Signature is not None:
            return sig_name(SigReader(srow.Signature.value, module).read_type())
    return f"TypeSpec#{rid}"


def _subst_key(subst: dict | None) -> str:
    if not subst:
        return ""
    return ",".join(f"{k}={sig_name(v)}" for k, v in sorted(subst.items()))


def sig_size(sig: dict | None, subst: dict | None, stack: tuple) -> tuple[int, int]:
    """(size, alignment) for a decoded field type."""
    if not sig:
        return PTR, 1
    k = sig["k"]
    if k == "prim":
        s = PRIM_SIZE.get(sig["name"], PTR)
        if s == 0:
            return 0, 1
        return s, min(s, 8)
    if k == "unknown":
        return PTR, PTR
    if k in ("ptr", "fnptr", "szarray", "array", "class", "byref"):
        return PTR, PTR
    if k in ("var", "mvar"):
        if subst and sig["index"] in subst:
            return sig_size(subst[sig["index"]], None, stack)
        return PTR, PTR                       # unresolved generic parameter
    if k == "valuetype":
        ref = sig["module"].resolve_token(sig["token"])
        if isinstance(ref, dict):
            return sig_size(ref, subst, stack)
        if ref is None:
            return PTR, PTR
        lay = type_layout(ref, None, stack)
        return lay["size"], lay["align"]
    if k == "ginst":
        if sig["kind"] == "class":
            return PTR, PTR
        ref = sig["module"].resolve_token(sig["token"])
        if isinstance(ref, dict):
            return sig_size(ref, subst, stack)
        if ref is None:
            return PTR, PTR
        lay = type_layout(ref, {i: a for i, a in enumerate(sig["args"])}, stack)
        return lay["size"], lay["align"]
    return PTR, PTR


def _base_ref(rec: TypeRec) -> TypeRec | None:
    bs = rec.base_sig
    if bs is None:
        return None
    name = bs.get("name", "")
    if not name or name in ("System.Object", "System.ValueType", "System.Enum"):
        return None
    return registry_lookup(name)


def type_layout(rec: TypeRec, subst: dict | None = None, stack: tuple = ()) -> dict:
    key = (rec.full, rec.module.name, _subst_key(subst))
    if key in rec._layout_cache:
        return rec._layout_cache[key]
    if key in stack:                            # cycle guard
        return {"size": PTR, "align": 1, "fields": [], "static_fields": [],
                "cycle": True}
    stack = stack + (key,)

    pack = rec.pack if rec.pack else DEFAULT_PACK
    explicit = rec.explicit
    out_fields = []
    cur, maxalign = 0, 1
    stat_cur, stat_align = 0, 1

    if rec.is_iface:
        out = {"size": 0, "align": 1, "fields": [], "static_fields": []}
        rec._layout_cache[key] = out
        return out

    # classes inherit the base field region first
    if not rec.is_struct and not rec.is_enum and not explicit:
        base = _base_ref(rec)
        if base is not None:
            bl = type_layout(base, subst, stack)
            cur, maxalign = bl["size"], max(1, bl["align"])

    for f in rec.fields:
        sig = f["sig"]
        if f["literal"]:
            size, align = 0, 1
        else:
            size, align = sig_size(sig, subst, stack)
        recorded = rec.field_layout.get(f["row"])

        if f["static"]:
            if f["literal"]:
                off = None
            elif subst:
                off = None                      # generic statics: runtime-assigned
            elif recorded is not None:
                off = recorded
            else:
                a = max(1, min(align, pack))
                off = rnd(stat_cur, a)
                stat_cur = off + size
                stat_align = max(stat_align, a)
            entry = {
                "name": f["name"],
                "type": ("const " + sig_name(sig)) if f["literal"] else sig_name(sig),
                "offset": off, "size": size,
                "token": field_token(f["row"]), "static": True,
            }
            if f["literal"]:
                entry["value"] = f.get("cvalue")
            if f.get("rva"):
                entry["data_rva"] = f"0x{f['rva']:08x}"
                if "sdata" in f:
                    entry["data_value"] = f["sdata"]
            out_fields.append(entry)
            continue

        a = max(1, min(align, pack))
        if recorded is not None:
            off = recorded
        else:
            off = rnd(cur, a)
        cur = max(cur, off + size)
        maxalign = max(maxalign, a)
        entry = {
            "name": f["name"], "type": sig_name(sig), "offset": off,
            "size": size, "token": field_token(f["row"]), "static": False,
        }
        if f.get("rva"):
            entry["data_rva"] = f"0x{f['rva']:08x}"
            if "sdata" in f:
                entry["data_value"] = f["sdata"]
        out_fields.append(entry)

    size = rnd(cur, maxalign)
    if rec.class_size and rec.class_size > size:
        size = rec.class_size

    inst = sorted((f for f in out_fields if not f["static"]),
                  key=lambda f: f["offset"])
    stat = sorted((f for f in out_fields if f["static"]),
                  key=lambda f: f["offset"] if f["offset"] is not None else 1 << 40)

    out = {"size": size, "align": maxalign, "fields": inst, "static_fields": stat}
    rec._layout_cache[key] = out
    return out


# --------------------------------------------------------------------------- #
#  dump
# --------------------------------------------------------------------------- #
def dump_module(mod: Module) -> dict:
    types = []
    for rec in mod.typedefs:
        if rec is None:
            continue
        if rec.is_enum:
            kind = "enum"
        elif rec.is_struct and rec.explicit:
            kind = "explicit-struct"
        elif rec.is_struct:
            kind = "struct"
        elif rec.is_iface:
            kind = "interface"
        elif rec.explicit:
            kind = "explicit-class"
        else:
            kind = "class"

        lay = type_layout(rec)
        entry = {
            "name": rec.full,
            "kind": kind,
            "base": sig_name(rec.base_sig) if rec.base_sig else None,
            "instance_size": lay["size"],
            "fields": lay["fields"],
            "static_fields": lay["static_fields"],
            "token": f"0x{0x02000000 | rec.idx:08x}",
            "methods": [
                {"name": m["name"], "il_rva": f"0x{m['rva']:08x}",
                 "token": method_token(m["row"])}
                for m in rec.methods
            ],
        }
        if kind == "enum":
            entry["members"] = [{"name": f["name"], "value": f.get("cvalue")}
                                for f in rec.fields if f["literal"]]
        types.append(entry)

    return {
        "assembly": mod.name,
        "runtime": "Mono (Unity), 64-bit",
        "pointer_size": PTR,
        "offset_basis": "instance fields are relative to the start of the "
                        "object's field data (see README)",
        "types": types,
    }


def main() -> int:
    args = sys.argv[1:]
    game_only = "--game-only" in args
    positional = [a for a in args if not a.startswith("-")]

    if not resolve_paths(positional[0] if positional else None):
        print("Airsoft Online installation not found — looked in the script's "
              "folder and in every Steam library for 'Airsoft Online Demo' / "
              "'Airsoft Online Beta'. Pass the game folder as argument.",
              file=sys.stderr)
        return 1
    print(f"game:   {GAME_ROOT}")
    print(f"output: {OUT}")

    modules, failed = [], []
    for path in sorted(MANAGED.glob("*.dll")):
        try:
            modules.append(Module(path))
        except Exception as e:
            failed.append(f"{path.name}: {e}")

    index, errors = [], list(failed)
    for mod in modules:
        game = is_game_assembly(mod.name)
        if game_only and not game:
            continue
        try:
            data = dump_module(mod)
            out = OUT / (Path(mod.name).stem + ".json")
            out.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            index.append({
                "assembly": mod.name, "file": out.name,
                "game": game,
                "types": len(data["types"]),
                "instance_fields": sum(len(t["fields"]) for t in data["types"]),
                "static_fields": sum(len(t["static_fields"]) for t in data["types"]),
            })
        except Exception:
            errors.append(f"{mod.name}:\n{traceback.format_exc()}")

    (OUT / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"wrote {len(index)} assembly dumps to {OUT}")
    for e in errors:
        print("ERROR:", e, file=sys.stderr)
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())