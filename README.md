# kcm — Linux kernel configuration management tool

`kcm.py` dumps a kernel `.config` to CSV (with metadata parsed from the
Kconfig tree) for viewing or editing in spreadsheet apps such as Excel, and
manages per-config memos (annotations) in a separate CSV that persists
across dumps.

## License

- (C)Copyright 2026 by Hiroshi Takekawa
- SPDX-License-Identifier: GPL-2.0-only
- Note that the license is GPLv2 only, no later option.

## Requirements

- Python 3.9+
- [kconfiglib](https://github.com/ulfalizer/kconfiglib) >= 14

```sh
pip install -r requirements.txt
```

## CSV format

`dump` produces one row per config option found in the `.config` (in
`.config` order, including `# CONFIG_FOO is not set` entries with value `n`):

| column    | meaning                                                        |
|-----------|----------------------------------------------------------------|
| `name`    | `CONFIG_`-prefixed option name                                 |
| `type`    | Kconfig type: `bool`, `tristate`, `string`, `int`, `hex`, `choice` |
| `title`   | the Kconfig prompt text (e.g. `Local version - append to kernel release`) |
| `value`   | value from the `.config` (`y`, `n`, number, or string without quotes) |
| `default` | all Kconfig defaults, `;`-joined; conditional ones as `value if CONDITION` |
| `depends` | full dependency expression, including enclosing `if` menu context. `<choice>` means "visible when the choice is" |
| `memo`    | annotation from a memo CSV, or empty                           |

Options present in the `.config` but not in the Kconfig tree (e.g. a newer
`.config` against an older source tree) get an empty `type`; the count is
reported on stderr.

## Commands

### dump

Parse a `.config` and a kernel source tree, write the CSV.

```sh
python3 kcm.py dump --config <.config> --srcdir <kernel-tree> -o dump.csv
python3 kcm.py dump --config .config --srcdir linux --memo memo.csv -o dump.csv
```

| option     | default      | description                                          |
|------------|--------------|------------------------------------------------------|
| `--config` | (required)   | path to the `.config` file                           |
| `--srcdir` | (required)   | path to the kernel source tree (contains `Kconfig`)  |
| `--memo`   | none         | memo CSV to pre-fill the `memo` column               |
| `-o`       | stdout       | output CSV file                                      |
| `--arch`   | `x86_64`     | target architecture (also sets `SRCARCH` correctly)  |
| `--cc`     | `$CC` or `gcc` | C compiler used for Kconfig `cc-option` checks    |
| `--ld`     | `$LD` or `ld`  | linker used for Kconfig checks                     |

`--arch`/`--cc`/`--ld` only affect parsing of the Kconfig tree (e.g.
arch-specific `source` paths and `cc-option` probes); they do not change the
values, which always come from the `.config`.

### memo-merge

Fill the `memo` column of an existing dump CSV from a memo CSV. The memo file
wins when both sides have a note.

```sh
python3 kcm.py memo-merge --csv dump.csv --memo memo.csv -o dump-annotated.csv
```

### memo-split

Extract non-empty `memo` cells from an annotated CSV back into a standalone
memo CSV (`name,note` header). This is how memos are persisted between dumps.

```sh
python3 kcm.py memo-split --csv dump-annotated.csv -o memo.csv
```

## Typical workflow

```sh
# 1. Initial dump (no memos yet)
python3 kcm.py dump --config .config --srcdir linux -o dump.csv

# 2. Open dump.csv in Excel, write notes in the memo column, save
#    (keep the file as CSV so kcm can read it back)

# 3. Persist the notes
python3 kcm.py memo-split --csv dump.csv -o memo.csv

# 4. Later: re-dump after kernel source or .config changed, memos come back
python3 kcm.py dump --config .config --srcdir linux --memo memo.csv -o dump.csv

# 5. Or merge memos into an existing dump without re-parsing the tree
python3 kcm.py memo-merge --csv dump.csv --memo memo.csv -o dump-annotated.csv
```

Memo CSV format is plain two columns; `CONFIG_` prefixes are optional and
case is matched exactly:

```csv
name,note
CONFIG_DEBUG_INFO,keep enabled for crash dumps
LOCALVERSION,set to -local for vendor builds
```

Duplicate names in a memo CSV: the last entry wins (a warning is printed).

## Notes

- The Kconfig tree is parsed with `warn=False`; if parsing fails, check that
  `--srcdir` points at a full kernel tree and that `--arch`/`--cc`/`--ld`
  match the compilers available on the system.
- Kernel 7.x introduced Kconfig syntax not yet known to kconfiglib
  (`transitional`, `modules`, and conditional `depends on X if Y`). `kcm`
  rewrites these on the fly: `depends on X if Y` becomes the equivalent
  `(!Y || X)`, which may appear in the `depends` column.
- Choice members (e.g. `CONFIG_HZ_250`) are typed `choice` and show
  `depends = <choice>`; the choice itself (e.g. `CONFIG_HZ`) shows the
  conditional defaults of its members.
- All progress/warning messages go to stderr, so stdout is always clean CSV
  when `-o` is omitted.
