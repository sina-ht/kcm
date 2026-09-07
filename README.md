# kcm — Linux kernel configuration management tool

`kcm.py` dumps a kernel `.config` to CSV (with metadata parsed from the
Kconfig tree) for viewing or editing in spreadsheet apps such as Excel,
reports the differences between two `.config` files, and manages per-config
memos (annotations) in a separate CSV that persists across dumps.

## License

- (C)Copyright 2026 by Hiroshi Takekawa
- SPDX-License-Identifier: GPL-2.0-only
- Note that the license is GPLv2 only, no later option.

## Requirements

- Python 3.9+
- [kconfiglib](https://github.com/ulfalizer/kconfiglib) >= 14
- [openpyxl](https://openpyxl.readthedocs.io/) >= 3.1 (only for `.xlsx` files)

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
| `diff`    | delta against another `.config` (filled by `diff-merge`); empty otherwise |
| `default` | all Kconfig defaults, `;`-joined; conditional ones as `value if CONDITION` |
| `depends` | full dependency expression, including enclosing `if` menu context. `<choice>` means "visible when the choice is" |
| `memo`    | annotation from a memo CSV, or empty                           |

Options present in the `.config` but not in the Kconfig tree (e.g. a newer
`.config` against an older source tree) get an empty `type`; the count is
reported on stderr.

The same columns are used for `.xlsx` output (see [Excel workbooks](#excel-workbooks-xlsx)).

## Commands

### dump

Parse a `.config` and a kernel source tree, write the CSV (or `.xlsx`).

```sh
python3 kcm.py dump --config .config --srcdir linux -o dump.csv
python3 kcm.py dump --config .config --srcdir linux --memo memo.csv -o dump.csv
python3 kcm.py dump --config .config --srcdir linux -o book.xlsx --sheet v7.1
```

| option     | default      | description                                          |
|------------|--------------|------------------------------------------------------|
| `--config` | (required)   | path to the `.config` file                           |
| `--srcdir` | (required)   | path to the kernel source tree (contains `Kconfig`)  |
| `--memo`   | none         | memo CSV to pre-fill the `memo` column               |
| `--sheet`  | `config`     | sheet in the output workbook (`.xlsx` only)          |
| `-o`       | stdout       | output CSV file (or `.xlsx` workbook)                |
| `--arch`   | `x86_64`     | target architecture (also sets `SRCARCH` correctly)  |
| `--cc`     | `$CC` or `gcc` | C compiler used for Kconfig `cc-option` checks    |
| `--ld`     | `$LD` or `ld`  | linker used for Kconfig checks                     |

`--arch`/`--cc`/`--ld` only affect parsing of the Kconfig tree (e.g.
arch-specific `source` paths and `cc-option` probes); they do not change the
values, which always come from the `.config`.

### diff

Report the differences between two `.config` files as a human-readable list
on stdout (summary counts go to stderr). `--srcdir` appends the Kconfig title
of each changed symbol.

`--base`/`--new` also accept dump tables (`.csv` or `.xlsx`) instead of
`.config` files, detected by extension — e.g. to compare two sheets of the
same workbook:

```sh
python3 kcm.py diff --base .config --new .config.new
python3 kcm.py diff --base .config --new .config.new --srcdir linux
python3 kcm.py diff --base book.xlsx --base-sheet before \
                    --new book.xlsx --new-sheet after
```

```
changed (2):
  CONFIG_KERNEL_GZIP: n -> y
  CONFIG_IKCONFIG: m -> y
added (1):
  CONFIG_RUSTC_HAS_SPAN_FILE: +y
removed (1):
  CONFIG_DECOMPRESS_ZSTD: -y
```

### diff-merge

Fill the `diff` column of an existing dump CSV by comparing each row's
`value` against a (changed) `.config`. Symbols present only in the new
`.config` get an appended row, with metadata from the Kconfig tree when
`--srcdir` is given. Re-running with the same config as before clears
stale diff cells.

```sh
python3 kcm.py diff-merge --csv dump.csv --new .config.new --srcdir linux -o dump-diff.csv
python3 kcm.py diff-merge --csv book.xlsx --sheet before --new .config.new \
  -o book.xlsx --out-sheet before
python3 kcm.py diff-merge --csv book.xlsx --new .config.new -o book.xlsx \
  --history-note "switch initramfs compression to LZ4"
```

Diff cell format: `old -> new` (value changed), `+v` (added in the new
config), `-v` (removed from it), empty (unchanged).

When the output is a `.xlsx` workbook, the delta is also appended to the
workbook's `History` sheet (see [History sheet](#history-sheet)):
`--no-history` suppresses this, and `--history-note TEXT` stamps a note on
every entry of the run.

### diff-split

Extract the non-empty `diff` cells from an annotated CSV into a standalone
diff CSV (`name,diff` header), so the recorded delta can be kept and
versioned separately.

```sh
python3 kcm.py diff-split --csv dump-diff.csv -o diff.csv
```

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

## Diffing configs

After re-generating a `.config` (e.g. via `make menuconfig`), review and
record what changed:

```sh
# 1. Review the delta (optionally with Kconfig titles)
python3 kcm.py diff --base .config --new .config.new --srcdir linux

# 2. Record the delta in the spreadsheet's diff column
python3 kcm.py diff-merge --csv dump.csv --new .config.new --srcdir linux -o dump-diff.csv

# 3. Persist the delta as a standalone file
python3 kcm.py diff-split --csv dump-diff.csv -o diff.csv
```

## Excel workbooks (.xlsx)

All table files (`.csv`) can also be `.xlsx` workbooks, detected by
extension; `openpyxl` is required for these. A typical workflow keeps one
workbook per project with one sheet per config snapshot:

```sh
python3 kcm.py dump --config .config --srcdir linux -o book.xlsx --sheet before
# ... change the config ...
python3 kcm.py dump --config .config.new --srcdir linux -o book.xlsx --sheet after
python3 kcm.py diff --base book.xlsx --base-sheet before --new book.xlsx --new-sheet after
python3 kcm.py diff-merge --csv book.xlsx --sheet before --new .config.new \
  -o book.xlsx --out-sheet before
python3 kcm.py memo-split --csv book.xlsx --sheet after -o memo.csv
```

How sheets are handled:

- **Writing** (`-o book.xlsx`): a new workbook gets a single sheet; an
  existing workbook keeps all its other sheets. The sheet name defaults to
  `config` (`--sheet` for `dump`, `--out-sheet` elsewhere); an existing
  sheet with that name is replaced. New sheets are appended at the end.
- **Reading** (`--csv book.xlsx` / `--base` / `--new`): the sheet is given
  by `--sheet` (or `--base-sheet`/`--new-sheet` for `diff`); without it the
  **first** sheet of the workbook is used.
- Sheets written by `kcm` get a bold header row, a frozen first row,
  autofilter, and sized columns. All cells are written as text.
- `.xlsx` output requires `-o` (it cannot go to stdout).

Caveats: cells are read back as text, so values survive round trips — but a
value you *retype* in the Excel UI may be stored as a number (fine for
integers, lossy for strings with leading zeros). Saving through `kcm`
preserves data and basic formatting of the other sheets, but exotic content
(charts, macros, custom drawings) may be degraded by openpyxl.

## History sheet

`diff-merge` writing to a `.xlsx` workbook appends one row per changed
symbol to the workbook's `History` sheet:

| column  | meaning                                                        |
|---------|----------------------------------------------------------------|
| `date`  | when the `diff-merge` ran (real Excel datetime, local time)    |
| `sheet` | the table sheet that was updated                               |
| `name`  | the `CONFIG_` symbol                                           |
| `diff`  | same format as the diff column: `old -> new`, `+v`, `-v`       |
| `note`  | from `--history-note`, or empty for manual annotation          |

The sheet is created on first use and only ever grows (one batch per run,
tagged by date), so it is the persistent record of config changes — data
sheets are replaced on re-dump, the history is not. A run with no changes
appends nothing; `--no-history` skips the record entirely. Don't name a
data sheet `History`: `diff-merge` refuses to record history when the
output sheet has that name.

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
