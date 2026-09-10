# kcm — Linux kernel configuration management tool

`kcm.py` dumps a kernel `.config` to CSV (with metadata parsed from the
Kconfig tree) for viewing or editing in spreadsheet apps such as Excel,
reports the differences between two `.config` files, and manages per-config
memos (annotations) and a change history in separate CSVs that persist
across dumps. `commit` records the committed-to-working change and git-commits
it in one step, and `history` pretty-prints the audit trail (with per-batch
patches). A `.kcmrc` project file supplies the paths so the common commands
need no flags.

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

## Config inputs: file paths or git revs

`dump --config`, and the `--base`/`--new` of `diff` and `diff-merge`, each
accept a **file path** or a **git rev**:

- a path that exists on disk is used as-is;
- otherwise the argument is a git rev, read via `git show`:
  `HEAD:linux/.config`, `main:linux/.config`, `v6.5:linux/.config`, or a bare
  `HEAD`/`main`/`@{upstream}` which borrows the path from the other operand
  (so `--base HEAD --new linux/.config` reads `HEAD:linux/.config`).

Git runs from the **project root** (the directory of the discovered `.kcmrc`,
or the current directory when there is none), so relative paths and `git show`
resolve from there — run `kcm` from anywhere in the repo. This is what makes
the git-native workflow work without temp files: diff and record straight
against `HEAD`.

## Project file (`.kcmrc`)

A `.kcmrc` records the project's paths and defaults once, so the common
commands need no flags (like `.git/config`). It is an INI file with a `[kcm]`
section, discovered by **walking up** from the current directory; its
directory becomes the project root. Precedence is **CLI flag > `.kcmrc` >
built-in default**. Commit it to the repo (it is project config).

```ini
[kcm]
config  = linux/.config      # the working .config (also the default --new)
srcdir  = linux              # kernel source tree (contains Kconfig)
xlsx    = kernel.xlsx        # the workbook (dump/diff-merge/commit output)
memo    = memos.csv          # memo CSV (name,note)
history = history.csv        # history CSV (the audit trail)
# base    = HEAD:linux/.config   # default; equals HEAD:<config>
# arch    = x86_64               # optional: arch / cc / ld
```

Keys: `config`, `srcdir`, `base`, `xlsx`, `sheet`, `memo`, `history`, and
optionally `arch`/`cc`/`ld`. Two defaults are computed for you: `--new`
defaults to `config`, and `--base` defaults to `HEAD:<config>` — so with a
`.kcmrc` in place, `kcm diff`, `kcm diff-merge`, and `kcm commit` all mean
"the change from the committed config to the working one".

Control discovery with `--rc PATH` (use a specific file) or `--no-rc`
(disable it).

## Commands

### dump

Parse a `.config` and a kernel source tree, write the CSV (or `.xlsx`).

```sh
python3 kcm.py dump --config .config --srcdir linux -o dump.csv
python3 kcm.py dump --config .config --srcdir linux --memo memo.csv -o dump.csv
# build the full workbook view from the text sources (memos + history)
python3 kcm.py dump --config .config --srcdir linux --memo memo.csv \
  --history history.csv -o book.xlsx
```

| option     | default      | description                                          |
|------------|--------------|------------------------------------------------------|
| `--config` | (required)   | path to the `.config` file, or a git rev (see [Config inputs](#config-inputs-file-paths-or-git-revs)) |
| `--srcdir` | (required)   | path to the kernel source tree (contains `Kconfig`)  |
| `--memo`   | none         | memo CSV to pre-fill the `memo` column               |
| `--history`| none         | history CSV to build the `History` sheet from (`.xlsx` only) |
| `--sheet`  | `config`     | sheet in the output workbook (`.xlsx` only)          |
| `-o`       | stdout       | output CSV file (or `.xlsx` workbook)                |
| `--arch`   | `x86_64`     | target architecture (also sets `SRCARCH` correctly)  |
| `--cc`     | `$CC` or `gcc` | C compiler used for Kconfig `cc-option` checks    |
| `--ld`     | `$LD` or `ld`  | linker used for Kconfig checks                     |

`--arch`/`--cc`/`--ld` only affect parsing of the Kconfig tree (e.g.
arch-specific `source` paths and `cc-option` probes); they do not change the
values, which always come from the `.config`. `--history` replaces the
`History` sheet with the given CSV (it is a build, not an append); a memo
entry with no matching config option is ignored with a warning on stderr.

### diff

Report the differences between two `.config` files as a human-readable list
on stdout (summary counts go to stderr). `--srcdir` appends the Kconfig title
of each changed symbol.

`--base`/`--new` accept file paths, git revs (see [Config inputs](#config-inputs-file-paths-or-git-revs)), or dump tables (`.csv` or `.xlsx`, detected by extension) — e.g. to compare two sheets of the same workbook:

```sh
python3 kcm.py diff --base .config --new .config.new
python3 kcm.py diff --base .config --new .config.new --srcdir linux
python3 kcm.py diff --base HEAD:linux/.config --new linux/.config --srcdir linux
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

### diff --patch

With `--patch` (or `-u`), `diff` emits a git-style **unified diff** of the two
configs on stdout instead of the report (both operands must be `.config`
inputs, not tables). The counts still go to stderr, and the exit status is
`1` when the configs differ, `0` when they are identical — so it is usable in
scripts. The output is `git apply`-compatible (`a/`/`b/` path prefixes):

```sh
python3 kcm.py diff --base HEAD:linux/.config --new linux/.config --patch > change.patch
git apply --check change.patch
```

### diff-merge

Update a dump table against a (changed) config and record the change. The
delta is computed between a base and the new config:

- **git-native (recommended):** give both as text configs with `--base` and
  `--new`. The output table is rebuilt to reflect the new (current) config,
  so it is always a clean view of "what is in the kernel now".
- **table base:** give `--csv <table>` (with no `--base`); the table's
  `value` column is the base and its `memo` column is carried forward
  (unless overridden by `--memo`).

The output table has one row per symbol in the new config, with the `diff`
column showing this run's change and (when `--srcdir` is given) Kconfig
metadata. Removed symbols are not kept as rows — they are recorded in the
history only.

```sh
# git-native: record the change and append a history batch
python3 kcm.py diff-merge --base .config --new .config.new --srcdir linux \
  -o book.xlsx --history history.csv --history-note "trim attack surface"
# git-native straight against HEAD (no temp file); the batch records the base commit
python3 kcm.py diff-merge --base HEAD:linux/.config --new linux/.config --srcdir linux \
  -o book.xlsx --history history.csv --history-note "trim attack surface"
# apply a memo CSV to the output
python3 kcm.py diff-merge --base .config --new .config.new --memo memo.csv \
  -o dump.csv
# table base (in place): base and memos come from the existing table
python3 kcm.py diff-merge --csv book.xlsx --new .config.new \
  -o book.xlsx --out-sheet before
```

Diff cell format: `old -> new` (value changed), `+v` (added in the new
config), `-v` (removed from it), empty (unchanged).

Each run with changes is recorded as a **history batch** (see
[History sheet](#history-sheet)): appended to the `History` sheet for
`.xlsx` output and/or to the `--history` CSV. `--no-history` records
nothing; `--history-note TEXT` stamps a note on every entry. When `--base`
and/or `--new` is a git rev, the batch also records the corresponding commit
hash(es) in `base_commit`/`new_commit` (empty when the input is an
uncommitted worktree or a plain path). A memo entry with no matching config
option is ignored with a warning on stderr.

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

### history-split

Extract the `History` sheet of a workbook back into a history CSV
(`batch,date,base_commit,new_commit,sheet,name,diff,note`). The inverse of
`dump --history`, and how a change history is persisted out of a workbook.

```sh
python3 kcm.py history-split --csv book.xlsx -o history.csv
```

### commit

One idempotent step that does the record-and-commit loop: diff the working
config against the base (default `HEAD:<config>`), rebuild the workbook view,
refresh the memo CSV, append a history batch, then `git add`/`git commit` the
text sources. With a `.kcmrc` in place it takes only a note:

```sh
# with .kcmrc (config/srcdir/memo/history all come from it)
python3 kcm.py commit --note "trim attack surface"
# fully explicit
python3 kcm.py commit --config linux/.config --base HEAD:linux/.config \
  --srcdir linux --memo memos.csv --history history.csv -o kernel.xlsx \
  --note "REQ-1234: enable FIPS crypto support"
```

Behavior:

- **No changes** (config matches the base) → prints a notice and exits `0`
  without touching anything (safe to re-run).
- Commits the **text sources of truth** — the `.config`, `history.csv`, and
  (if any memos) `memos.csv`. The commit message is the `--note` (or
  `Update <config>`) plus greppable trailers:
  ```
  kcm-batch: 3f9a2c1d8b4e
  kcm-base:  26d4c7f…
  kcm-delta: 13 changed, 5 added, 1 removed
  ```
- The workbook is **written locally** (as the current view) when a path is set
  (`-o`/`.kcmrc` `xlsx`) but is **not** committed by default — it is a derived
  binary. Add `--commit-xlsx` to stage and commit it too.
- `--no-git` writes the memos/history/workbook but skips `git`; `--no-memo`
  skips the memo refresh; `--dry-run` reports the change and files without
  writing or committing; `--signoff`/`-s` adds `Signed-off-by`.

### history

Pretty-print the change history from `history.csv`. `history` alone lists
batches (like `git log`); `show` expands one.

```sh
python3 kcm.py history log                 # one line per batch, newest first
python3 kcm.py history log --limit 5 --long
python3 kcm.py history show 3f9a2c         # one batch: per-symbol old -> new
python3 kcm.py history show 3f9a2c --patch # + the git unified diff for the batch
python3 kcm.py history log --json
```

`log` prints `<batch>  <date>  base=<sha7>  new=<sha7>  <n>c <a>a <r>r  <note>`;
`--reverse` for oldest-first, `--long` for a block per batch, `--json` for clean
machine-readable output. `show <batch>` takes a full or unique-prefix batch id
and lists each changed symbol with its `diff` cell (this needs no git). `--patch`
rebuilds the batch as a git unified diff on stdout (base = `<base_commit>:<config>`,
new = `<new_commit>:<config>` or `HEAD:<config>`; override with `--base`/`--new`),
moving the human report to stderr so the patch stays clean.

## Typical workflow

For an end-to-end guide to building and maintaining a kernel config as a
living Excel workbook (baseline, annotation, and change tracking), see
[WORKFLOW.md](WORKFLOW.md). The minimal CSV round trip is:

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

After re-generating a `.config` in the tree (e.g. via `make menuconfig`), the
committed version at `HEAD` is the base and the working-tree file is the new
state. Review and record what changed, straight against git (no temp files):

```sh
# 1. Review the delta (optionally with Kconfig titles)
python3 kcm.py diff --base HEAD:linux/.config --new linux/.config --srcdir linux

# 2. Record the delta: rebuild the table, fill the diff column,
#    and append a history batch (base_commit is stamped automatically)
python3 kcm.py diff-merge --base HEAD:linux/.config --new linux/.config \
  --srcdir linux -o book.xlsx --history history.csv --history-note "..."

# 3. Emit the change as a git-applicable patch (for review / PRs)
python3 kcm.py diff --base HEAD:linux/.config --new linux/.config --patch > change.patch

# 4. Persist just the current delta as a standalone file
python3 kcm.py diff-split --csv book.xlsx -o diff.csv
```

Steps 2 and the `git commit` collapse into one idempotent command —
`kcm commit --note "..."` (see [commit](#commit)); with a `.kcmrc` in place it
is just `kcm commit --note "..."` and it also refreshes the memo CSV.

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

`diff-merge` records each run's changes as a **batch**: one row per changed
symbol, all sharing a `batch` id, a timestamp, the `--history-note`, and —
when the inputs are git revs — the base/new commit hashes. The batch is
appended to the workbook's `History` sheet (for `.xlsx` output) and/or to a
history CSV (`--history`):

| column        | meaning                                                        |
|---------------|----------------------------------------------------------------|
| `batch`       | per-run id (12-hex-char token); every row of a run shares it   |
| `date`        | when the `diff-merge` ran (Excel datetime in the sheet; local time) |
| `base_commit` | git commit the `--base` came from (full SHA; empty if not a git rev) |
| `new_commit`  | git commit the `--new` came from (full SHA; empty for an uncommitted worktree or a non-rev input) |
| `sheet`       | the table sheet that was updated                               |
| `name`        | the `CONFIG_` symbol                                           |
| `diff`        | same format as the diff column: `old -> new`, `+v`, `-v`       |
| `note`        | from `--history-note`, or empty                                |

The commit columns link each batch back to `git`: `history show <batch> --patch`
reconstructs the batch as a git patch (or do it by hand with
`diff --base <base_commit>:linux/.config --new <next>:linux/.config --patch`).
Read the trail without Excel with `history log` (list) and `history show
<batch>` (one batch's per-symbol changes) — see [history](#history).

The history is the persistent record of config changes: data sheets are
replaced on re-dump, the history is not. The `History` sheet is created on
first use and only ever grows (one batch appended per run); a run with no
changes appends nothing, and `--no-history` skips the record entirely.

The history **CSV is the text source of truth** for the history (see
[WORKFLOW.md](WORKFLOW.md)). `dump --history FILE` rebuilds the `History`
sheet from it, and `history-split` extracts it back out, so the workbook's
history is a regenerable view of the CSV. Don't name a data sheet
`History`: `diff-merge`/`dump` refuse to record or build history when a
data sheet has that name.

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
