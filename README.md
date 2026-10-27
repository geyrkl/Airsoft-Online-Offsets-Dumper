# Airsoft Online Offsets Dumper

Extracts managed metadata offsets (field layouts, static fields, enum values,
constants, method tokens) from **Airsoft Online** (Unity / Mono, 64-bit) —
works with both the **Demo** and the **Beta** installation.

## Quick start

```bash
pip install -r requirements.txt
python extract_offsets.py
```

### ✅ Automatic output folder

**You don't have to configure anything.** When you run the python file it
**automatically creates an `offsets` folder inside your Airsoft Online
installation directory** and writes all offset files there.

The script finds the game install on its own:

1. If the script is placed inside a game folder (Demo or Beta), that folder is
   used and the `offsets` folder is created next to it.
2. Otherwise it reads the Steam `libraryfolders.vdf` and scans **every Steam
   library** for `Airsoft Online Demo` / `Airsoft Online Beta`.
3. You can also pass the game folder explicitly: `python extract_offsets.py "C:\...\Airsoft Online Beta"`

Example result:

```
C:\Program Files (x86)\Steam\steamapps\common\Airsoft Online Demo\
└── offsets\                  <- created automatically
    ├── index.json            <- overview of all dumped assemblies
    ├── Assembly-CSharp.json
    ├── Pixomnia.AirsoftOnline.Game.json
    ├── Fusion.Runtime.json
    └── ... (one <AssemblyName>.json per assembly)
```

## Usage

```bash
python extract_offsets.py              # dump ALL assemblies (game + engine)
python extract_offsets.py --game-only  # only game assemblies (skip engine/framework)
python extract_offsets.py <gamedir>    # explicit path to the game folder
python _validate.py                    # ground-truth + sanity checks
```

Requirements: Python 3.9+ and `dnfile` (`pip install dnfile`).

## Output format

Each `<AssemblyName>.json` contains every type of that assembly:

- `name`, `kind` (class / struct / enum / interface / explicit-*), `base`,
  `instance_size`, metadata `token`
- `fields` — instance fields with `name`, `type`, `offset`, `size`, `token`
- `static_fields` — same, plus `value` for `const` literals and
  `data_rva` / `data_value` for FieldRva initializers
- `members` — enum member names with their constant values
- `methods` — `name`, `il_rva` (IL body offset in the PE file) and the
  metadata token (`0x06…`) for hooks / trampolines

### Layout model

- Pointer size 8 (x64 Mono). **Instance offsets are absolute from the managed
  object pointer** (16-byte Mono object header included, inherited base-class
  fields included) — e.g. `Health._CurrentHealth` is at `+128`.
- Layout rule: metadata field order, standard alignment (pack 8); recorded
  `FieldLayout` / `ClassLayout` values are used when present (authoritative
  for enums, explicit layouts).
- Static field offsets are relative to the per-class static data block
  (runtime-assigned; recorded values are used when present in metadata).

## Highlights (game side)

| Type | Offset | Meaning |
|---|---|---|
| `...Game.Health._CurrentHealth` | `+128` | current HP (ushort) |
| `...Player.Player.fpsController` | `+240` | FPSController reference |
| `...Player.Player._playerInfo` | `+224` | PlayerInfo reference |
| `...PlayerInfo._PlayerName` | `+120` | player name |
| `...PlayerInfo._Kills / _Deaths / _Score` | `+252 / +253 / +256` | scoreboard |
| `...PlayerInfo._playerPosition` | `+464` | Vector3 position |

## Notes

- Engine/framework assemblies (`UnityEngine.*`, `System.*`, Unity packages)
  are dumped by default; use `--game-only` to skip them.
- The layout is computed statically and validated against engine ground truth
  (`Vector4`, `Quaternion`, `Rect`, `Bounds` — all exact).
