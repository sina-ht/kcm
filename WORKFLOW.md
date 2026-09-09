# WORKFLOW.md — Maintaining a kernel config as a living Excel workbook

`kcm` lets you treat one Excel workbook as the single, auditable answer to
"what is in our kernel, and why". The workbook mirrors the kernel tree's
`.config`, carries your annotations in the `memo` column, and keeps a full
audit trail in the `History` sheet. This guide walks through the practical
loop an engineer follows to build and maintain that workbook.

## Conventions

- **One workbook per product/arch** — e.g. `kernel.xlsx`, kept next to the
  kernel tree (not inside it).
- **Working sheet** — `config` (the default sheet), updated in place as the
  kernel evolves.
- **Frozen baseline sheet (optional)** — e.g. `baseline`, a copy of the
  initial state you never update again, for later comparison.
- **Columns have fixed jobs**:
  - `memo` — your annotations / rationale. You own this; it persists across
    every merge.
  - `diff` — the change introduced by the *most recent* `diff-merge` (see
    [Keeping the workbook healthy](#keeping-the-workbook-healthy)).
  - `History` sheet — the append-only record of every change, the audit
    trail.

Keep the kernel tree's `.config` as the source of truth for *values*, and the
workbook as the source of truth for *annotations and history*. After every
change you make in the tree, run `diff-merge` so the two agree again.

---

## Stage 1 — Establish the baseline

You have a kernel tree with the config your kernel currently builds from
(a vendor defconfig, an existing `.config`, or `/proc/config.gz` from the
running system). Dump it to create the initial workbook:

```sh
python3 kcm.py dump --config linux/.config --srcdir linux -o kernel.xlsx
```

This creates `kernel.xlsx` with a `config` sheet: one row per symbol (~5000
for a full x86 config), in `.config` order, with type, title, value,
dependency, default, and an empty `memo` column.

Open it in Excel and get your bearings:

- Filter / sort / search to see what is actually enabled.
- Read the `title` column for the human description of each option.
- Read the `depends` column to understand *why* an option is present — many
  symbols are pulled in automatically by others, so disabling the parent is
  often the right move rather than the child.

Optionally, freeze a copy you will never touch again, so you can compare
against the starting point later:

```sh
python3 kcm.py dump --config linux/.config --srcdir linux -o kernel.xlsx --sheet baseline
```

Now `kernel.xlsx` has a `config` sheet (to be maintained) and a `baseline`
sheet (a frozen reference).

## Stage 2 — Annotate what you learn

As you review the config, record the "why" for the options that matter. Fill
the `memo` column directly in Excel and save — for example:

| name               | memo                                      |
|--------------------|-------------------------------------------|
| `CONFIG_DEBUG_INFO`| keep for crash dumps                      |
| `CONFIG_NF_CONNTRACK` | required by the firewall rules         |
| `CONFIG_USB_GADGET`| pulled in by the on-board USB-OTG driver  |

That's the whole annotation step. The notes live in the workbook and will
ride along with every later merge, so there is nothing to re-apply.

If you prefer to keep the notes as version-controlled text, maintain a memo
CSV (`name,note` — see the README for the format) and merge it in:

```sh
python3 kcm.py memo-merge --csv kernel.xlsx --sheet config --memo memos.csv \
  --out-sheet config
```

Tip: annotate early (right here, in Stage 1–2) rather than trying to recall
the rationale after the config has already moved on.

## Stage 3 — First change: trim the attack surface

Make the change in the kernel tree, not in the workbook:

```sh
cd linux
make menuconfig          # walk the tree, disable unused drivers/filesystems/net, etc.
make olddefconfig        # resolve the resulting dependencies
```

Then sync the workbook and record the change as a labeled history batch:

```sh
python3 kcm.py diff-merge --csv kernel.xlsx --sheet config --new linux/.config \
  --srcdir linux --out-sheet config \
  --history-note "attack-surface: disable unused filesystems, drivers, networking"
```

What just happened:

- The `config` sheet now matches the new `.config`.
- The `diff` column shows, for this batch, what changed
  (`old -> new`, `+v`, `-v`).
- A batch of entries was appended to the `History` sheet, every one tagged
  with the `--history-note` text and the run's date/time.

Open the workbook and filter the `config` sheet on a non-empty `diff` to
review the change; the `History` sheet now has a dated, labeled record of it.

## Stage 4 — A new requirement arrives

Repeat the loop, and capture the requirement's rationale in the memos.

1. Make the change in the tree:

   ```sh
   make menuconfig          # enable/tune what the requirement needs
   make olddefconfig
   ```

2. Sync and record it, naming the requirement in the history note:

   ```sh
   python3 kcm.py diff-merge --csv kernel.xlsx --sheet config --new linux/.config \
     --srcdir linux --out-sheet config \
     --history-note "REQ-1234: enable FIPS crypto support"
   ```

3. Fill or update the `memo` cells for the symbols this requirement touches,
   directly in Excel — e.g. for `CONFIG_CRYPTO_FIPS`, `REQ-1234: required for
   FIPS mode`.

The workbook now reflects the requirement, the `History` sheet shows the
whole change history with each batch labeled, and the `memo` column explains
the reasoning behind the non-obvious choices.

---

## Keeping the workbook healthy

- **Always `diff-merge` after a `menuconfig` session.** The workbook is a
  mirror of `.config` plus your annotations; the merge is the sync step that
  keeps them consistent and produces the history entry.
- **The `diff` column is transient.** It shows the most recent merge only.
  The persistent, cumulative record is the `History` sheet.
- **Compare against a frozen sheet any time**, e.g. everything that differs
  from the original baseline:

  ```sh
  python3 kcm.py diff --base kernel.xlsx --base-sheet baseline \
    --new kernel.xlsx --new-sheet config
  ```

- **Export just the changes** to review or version-control them separately:

  ```sh
  python3 kcm.py diff-split --csv kernel.xlsx --sheet config -o changes.csv
  ```

- **The `History` sheet is append-only** and lives in the same workbook —
  don't hand-edit it unless you intend to. `--no-history` skips recording a
  particular run; a run with no changes appends nothing.
- **Upgrading the kernel tree** works through the same loop: after pulling a
  new tree (new or renamed symbols), run `diff-merge` with `--srcdir` and the
  workbook picks up added symbols (with metadata) and marks removed ones, all
  recorded in `History`.
