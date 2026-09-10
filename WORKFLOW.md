# WORKFLOW.md — Maintaining a kernel config as a living, version-controlled workbook

`kcm` lets you treat a kernel config as a small set of **version-controlled
text files** plus a disposable Excel view. The text files are the source of
truth that you `git`; the `.xlsx` is a regenerable view you open to look at
and annotate the config.

## What is the source of truth

| source         | file                                        | holds                          |
|----------------|---------------------------------------------|--------------------------------|
| the config     | the tree's `.config` (post-`make olddefconfig`) | the actual values       |
| annotations    | `memos.csv` (`name,note`)                   | the "why" for each option      |
| change history | `history.csv` (`batch,date,sheet,name,diff,note`) | the audit trail      |

The `.xlsx` is a **projection** of those three (plus the Kconfig tree for
metadata). It holds no unique data: discard it and rebuild it any time with
`dump`. So the rule of thumb is — keep the three text files complete and
current in `git`, and regenerate the workbook whenever you want to look at or
edit the config. After every change, persist to the text files (that is the
commit-worthy state); the workbook is just what you are looking at.

## Conventions

- **One set of files per product/arch**: the tree's `.config`, `memos.csv`,
  `history.csv`, and a regenerable `kernel.xlsx`, all kept next to the tree.
- **Working sheet** — `config` (the default sheet); **frozen baseline sheet
  (optional)** — `baseline`, for comparison.
- **Columns have fixed jobs**: `memo` = your annotations, `diff` = the change
  from the most recent `diff-merge`, `History` sheet = the audit trail (a view
  of `history.csv`).

---

## Stage 1 — Establish the baseline

You have a kernel tree with the config your kernel currently builds from (a
vendor defconfig, an existing `.config`, or `/proc/config.gz` from the running
system). Commit that config, then build the workbook to look at it:

```sh
git add linux/.config && git commit -m "baseline config"
python3 kcm.py dump --config linux/.config --srcdir linux -o kernel.xlsx
```

`kernel.xlsx` gets a `config` sheet: one row per symbol (~5000 for a full x86
config), in `.config` order, with type, title, value, dependency, default, and
an empty `memo` column.

Open it in Excel and get your bearings:

- Filter / sort / search to see what is actually enabled.
- Read the `title` column for the human description of each option.
- Read the `depends` column to understand *why* an option is present — many
  symbols are pulled in by others, so disabling the parent is often the right
  move rather than the child.

Optionally, freeze a baseline sheet you will never touch again, so you can
compare against the starting point later:

```sh
python3 kcm.py dump --config linux/.config --srcdir linux -o kernel.xlsx --sheet baseline
```

## Stage 2 — Annotate what you learn

As you review the config, record the "why" for the options that matter. Edit
the `memo` column directly in Excel, then persist it to the text source of
truth and commit:

```sh
python3 kcm.py memo-split --csv kernel.xlsx -o memos.csv
git add memos.csv && git commit -m "annotate baseline config"
```

For example:

| name                 | memo                                 |
|----------------------|--------------------------------------|
| `CONFIG_DEBUG_INFO`  | keep for crash dumps                 |
| `CONFIG_NF_CONNTRACK`| required by the firewall rules       |
| `CONFIG_USB_GADGET`  | pulled in by the on-board USB-OTG driver |

`memo-split` writes only the symbols present in the current config, so
`memos.csv` never carries annotations for options that are no longer there.
From here on `memos.csv` is the durable home of the notes; the workbook's
`memo` column is just how you edit them.

Tip: annotate early (right here) rather than trying to recall the rationale
after the config has already moved on.

## Stage 3 — First change: trim the attack surface

Make the change in the kernel tree, not in the workbook:

```sh
cd linux
make menuconfig          # disable unused drivers/filesystems/networking, etc.
make olddefconfig        # resolve the resulting dependencies
cd ..
```

The config on disk is now the *new* state; the committed one at `HEAD` is the
*base*. Extract the base and record the change — rebuild the view, fill the
`diff` column, and append a labeled batch to the history (both `history.csv`
and the `History` sheet):

```sh
git show HEAD:linux/.config > /tmp/config.base
python3 kcm.py diff-merge --base /tmp/config.base --new linux/.config \
  --srcdir linux --memo memos.csv --history history.csv \
  --history-note "attack-surface: disable unused filesystems, drivers, networking" \
  -o kernel.xlsx
```

What just happened:

- `kernel.xlsx`'s `config` sheet now matches the new config; the `diff` column
  shows this run's changes (`old -> new`, `+v`). Removed symbols are recorded
  in the history, not kept as rows.
- A batch was appended to `history.csv` **and** the `History` sheet, every row
  sharing one `batch` id, the run's timestamp, and the `--history-note`.

Commit the two files that changed — the config (values) and the history:

```sh
git add linux/.config history.csv && git commit -m "trim attack surface"
```

`git diff` on `linux/.config` now shows exactly the value changes, and
`history.csv` is the labeled audit trail.

## Stage 4 — A new requirement arrives

Repeat the loop, and capture the requirement's rationale in the memos.

1. Make the change in the tree: `make menuconfig && make olddefconfig`.
2. Record it, naming the requirement in the history note:

   ```sh
   git show HEAD:linux/.config > /tmp/config.base
   python3 kcm.py diff-merge --base /tmp/config.base --new linux/.config \
     --srcdir linux --memo memos.csv --history history.csv \
     --history-note "REQ-1234: enable FIPS crypto support" -o kernel.xlsx
   ```

3. Annotate the symbols this requirement touches: edit their `memo` cells in
   Excel (e.g. `CONFIG_CRYPTO_FIPS` → `REQ-1234: required for FIPS mode`),
   then `python3 kcm.py memo-split --csv kernel.xlsx -o memos.csv`.
4. Commit: `git add linux/.config history.csv memos.csv && git commit -m "REQ-1234: FIPS"`.

The config reflects the requirement, `history.csv` shows the whole labeled
change history, and `memos.csv` explains the non-obvious choices.

---

## Keeping it version-controlled

- **The three text files are the state**: the `.config` (values), `memos.csv`
  (annotations), and `history.csv` (audit trail). Keep them complete and
  current in `git`; `kernel.xlsx` is disposable.
- **Always `diff-merge` after a `menuconfig` session.** It is the sync step
  that updates the view and appends the history batch.
- **The `diff` column is transient** (the last merge only); the `History`
  sheet / `history.csv` is the persistent record.
- **Rebuild the view any time** from the committed sources:

  ```sh
  python3 kcm.py dump --config linux/.config --srcdir linux \
    --memo memos.csv --history history.csv -o kernel.xlsx
  ```

- **Review a change** three ways: `git diff linux/.config` (the value
  changes), `history.csv` (labeled batches — filter by `batch` in Excel), or
  `kcm diff --base <old> --new <new> --srcdir linux` (a titled report).
- **Compare against a frozen baseline** any time:

  ```sh
  python3 kcm.py diff --base kernel.xlsx --base-sheet baseline \
    --new kernel.xlsx --new-sheet config
  ```

- **Export just the current delta** to review or version-control separately:

  ```sh
  python3 kcm.py diff-split --csv kernel.xlsx -o changes.csv
  ```

- **The history is append-only** and lives in `history.csv` (and the sheet).
  `--no-history` skips a run; a no-op run appends nothing.
- **Upgrading the kernel tree** uses the same loop: after pulling a new tree,
  run `diff-merge --base <old config> --new <new config> --srcdir <new tree>`
  and it records the added/removed/changed symbols in the history and rebuilds
  the view with fresh metadata.

## The base is just a text config

`diff-merge --base` takes any text `.config`, so the base can be whatever `git`
hands you: `git show HEAD:linux/.config` (or `<ref>:linux/.config`) written to
a temp file, or a copy you saved before the change. This is what makes the
delta git-native — you are diffing two committed configs, not the possibly
stale workbook. (Accepting git refs directly, and stamping the commit hash into
the history, are planned for a later release.)
