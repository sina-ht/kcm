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
| change history | `history.csv` (`batch,date,base_commit,new_commit,sheet,name,diff,note`) | the audit trail |

The `.xlsx` is a **projection** of those three (plus the Kconfig tree for
metadata). It holds no unique data: discard it and rebuild it any time with
`dump`. So the rule of thumb is — keep the three text files complete and
current in `git`, and regenerate the workbook whenever you want to look at or
edit the config. After every change, persist to the text files (that is the
commit-worthy state); the workbook is just what you are looking at.

## Conventions

- **`.kcmrc` holds the paths**: one small INI file next to the tree
  (`config`, `srcdir`, `memo`, `history`, `xlsx`) makes the commands below
  flag-free — with it in place, `kcm commit --note "..."` is the entire
  record-and-commit step. It is discovered by walking up from the current
  directory, so run `kcm` from anywhere in the repo (see
  [README](README.md#project-file-kcmrc)).
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
*base*. Record **and** commit the change in one idempotent step. `commit`
diffs the working config against `HEAD`, rebuilds the view, refreshes the
memos (overlaying any annotations you made in `kernel.xlsx`), appends a
labeled batch to the history, and git-commits the text sources — so the base
commit is stamped and the config, history, and memos travel in one commit:

```sh
python3 kcm.py commit --note "attack-surface: disable unused filesystems, drivers, networking"
```

(with a `.kcmrc` in place this needs no other flags; otherwise pass
`--config linux/.config --base HEAD:linux/.config --srcdir linux --memo
memos.csv --history history.csv -o kernel.xlsx`). Re-running it is a no-op
until the config moves again; `--dry-run` previews it.

What just happened:

- `kernel.xlsx`'s `config` sheet now matches the new config; the `diff` column
  shows this run's changes (`old -> new`, `+v`). Removed symbols are recorded
  in the history, not kept as rows.
- A batch was appended to `history.csv` **and** the `History` sheet, every row
  sharing one `batch` id, the run's timestamp, and the note; the batch's
  `base_commit` is stamped from `HEAD`.
- `linux/.config`, `history.csv`, and (if any) `memos.csv` were committed
  together, with the batch id and base commit in the commit message. (The
  workbook is written locally as your view but is not committed unless you
  pass `--commit-xlsx`.)

To do it in two steps instead — record, then commit yourself:

```sh
python3 kcm.py diff-merge --base HEAD:linux/.config --new linux/.config \
  --srcdir linux --memo memos.csv --history history.csv \
  --history-note "..." -o kernel.xlsx
git add linux/.config history.csv && git commit -m "trim attack surface"
```

`git diff` on `linux/.config` shows exactly the value changes, and
`history.csv` is the labeled audit trail.

## Stage 4 — A new requirement arrives

Repeat the loop, and capture the requirement's rationale in the memos.

1. Make the change in the tree: `make menuconfig && make olddefconfig`.
2. Annotate the symbols this requirement touches: edit their `memo` cells in
   `kernel.xlsx` (e.g. `CONFIG_CRYPTO_FIPS` → `REQ-1234: required for FIPS
   mode`).
3. Record and commit it in one step, naming the requirement in the note.
   `commit` picks the annotations up from the workbook, refreshes `memos.csv`,
   appends the history batch, and commits the text sources:

   ```sh
   python3 kcm.py commit --note "REQ-1234: enable FIPS crypto support"
   ```

The config reflects the requirement, `history.csv` shows the whole labeled
change history, and `memos.csv` explains the non-obvious choices.

---

## Keeping it version-controlled

- **The three text files are the state**: the `.config` (values), `memos.csv`
  (annotations), and `history.csv` (audit trail). Keep them complete and
  current in `git`; `kernel.xlsx` is disposable.
- **Always record the change after a `menuconfig` session** — `kcm commit
  --note "..."` (one step: view + memos + history + git commit) or
  `diff-merge` in two steps. It is the sync step that updates the view and
  appends the history batch.
- **The `diff` column is transient** (the last merge only); the `History`
  sheet / `history.csv` is the persistent record.
- **Rebuild the view any time** from the committed sources:

  ```sh
  python3 kcm.py dump --config linux/.config --srcdir linux \
    --memo memos.csv --history history.csv -o kernel.xlsx
  ```

- **Read the audit trail** without Excel: `kcm history log` (one line per
  batch, newest first) and `kcm history show <batch>` (every symbol a batch
  touched; add `--patch` for a git-applicable diff of just that batch).
- **Review a change** several ways: `git diff linux/.config` (the value
  changes), `kcm history log` / `kcm history show <batch>` (the labeled audit
  trail), `kcm diff --base <old> --new <new> --srcdir linux` (a titled
  report), or `kcm diff --base HEAD:linux/.config --new linux/.config
  --patch` (a git-applicable patch for review / PRs).
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

`diff-merge --base` takes any text `.config` — a file path or a git rev
(`HEAD:linux/.config`, `<ref>:linux/.config`), or a copy you saved before the
change. Pass the git rev directly (no temp file), which is what makes the
delta git-native: you diff two committed configs, not the possibly stale
workbook, and the batch records the base commit in `base_commit` for the
audit trail. Run `kcm` from inside the repo so `git show` finds it.
