#!/usr/bin/env python3
# kcm.py -- Linux kernel configuration management tool.
# (C)Copyright 2026 by Hiroshi Takekawa
#
# SPDX-License-Identifier: GPL-2.0-only
"""kcm - Linux kernel configuration management tool.

 Dumps .config entries (name, type, title, value, diff, default, depends)
to CSV or Excel (.xlsx, requires openpyxl) for viewing/editing in
spreadsheet apps, reports the differences between two .config files (or
dump tables), and manages per-config memos (annotations) in a separate
persistent CSV.

Config inputs (--config, --base, --new) may be a file path or a git rev
(<ref>:<path>, or a bare <ref> that borrows the sibling operand's path).

Diff cells (filled by 'diff-merge'): 'old -> new' (value changed), '+v'
(symbol added in the new config), '-v' (symbol removed from it).

Change history: 'diff-merge' records one row per changed symbol as a batch
(batch, date, base_commit, new_commit, sheet, name, diff, note), stamped
with a per-run batch id and the git commit(s) the inputs span (empty when
an input is an uncommitted worktree or plain path). The batch is appended
to the workbook's History sheet (.xlsx output) and/or a history CSV
(--history); 'dump --history' rebuilds the History sheet from a history
CSV, and 'history-split' extracts it back out.

Commands:
  dump         .config + Kconfig tree -> CSV/.xlsx (optionally with memos + history)
  diff         two configs -> change report, or a git unified diff (--patch)
  diff-merge   base + new config -> table with diff column + a history batch
  diff-split   dump table -> diff.csv (name,diff)
  memo-merge   table + memo.csv -> table with the memo column filled
  memo-split   annotated table -> memo.csv (name,note)
  history-split History sheet -> history.csv
  commit       record the committed->working change and git-commit it
  history      view the change history: 'log' (list) or 'show <batch>' (detail)

Project file: a .kcmrc (INI, [kcm] section) provides defaults for --config,
--srcdir, --base, --xlsx, --sheet, --memo, --history, --arch/--cc/--ld; CLI
flags always win. It is discovered by walking up from the current directory (its
directory becomes the project root, and relative paths and git run from there).
Use --rc PATH to name a specific file or --no-rc to disable it.
"""

import argparse
import configparser
import csv
import io
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime

COLUMNS = ["name", "type", "title", "value", "diff", "default", "depends", "memo"]
CONFIG_PREFIX = "CONFIG_"
DEFAULT_SHEET = "config"
HISTORY_SHEET = "History"
HISTORY_COLUMNS = [
    "batch",
    "date",
    "base_commit",
    "new_commit",
    "sheet",
    "name",
    "diff",
    "note",
]
HISTORY_DATE_FMT = "%Y-%m-%d %H:%M"

RC_NAME = ".kcmrc"
RC_SECTION = "kcm"
DEFAULT_MEMO = ".kcm-memos.csv"
DEFAULT_HISTORY = ".kcm-history.csv"

# Kernel 7.x Kconfig keywords/constructs not (yet) known to kconfiglib.
# Bare property lines that can be dropped without changing semantics.
SKIP_LINES = frozenset({"transitional", "modules"})
# 'depends on <expr> if <expr>' == 'depends on (!<expr2> || <expr1>)'
DEP_IF = re.compile(r"^(\s*depends\s+on\s+)(.*\S)\s+if\s+(.*\S)\s*$")
# Condition that is a single negated atom (e.g. '!FOO'): 'A if !B' -> '(B || A)'
NEG_ATOM = re.compile(r"^!([A-Za-z_][A-Za-z0-9_]*|\([^()]*\))$")

# ARCH -> SRCARCH mapping (see top-level Makefile)
SRCARCH_MAP = {"i386": "x86", "x86_64": "x86"}


class _KconfigCompat:
    """Line filter making kernel 7.x Kconfig files parseable by kconfiglib."""

    def __init__(self, f):
        self._f = f

    def readline(self):
        while True:
            line = self._f.readline()
            if line == "":
                return line
            if line.strip() in SKIP_LINES:
                continue
            stripped = line.rstrip()
            comment = stripped.find("#")
            expr = (stripped[:comment] if comment != -1 else stripped).rstrip()
            if expr.lstrip().startswith("depends on"):
                m = DEP_IF.match(expr)
                if m:
                    dep, cond = m.group(2), m.group(3)
                    neg = NEG_ATOM.match(cond)
                    if neg:
                        line = "{}({} || ({}))\n".format(
                            m.group(1), neg.group(1), dep
                        )
                    else:
                        line = "{}(!({}) || ({}))\n".format(
                            m.group(1), cond, dep
                        )
            return line

    def close(self):
        self._f.close()


def _install_kconfig_compat():
    import kconfiglib

    if getattr(_install_kconfig_compat, "_done", False):
        return
    orig_open = kconfiglib.Kconfig._open

    def _open(self, filename, mode):
        f = orig_open(self, filename, mode)
        return _KconfigCompat(f) if mode == "r" else f

    kconfiglib.Kconfig._open = _open
    _install_kconfig_compat._done = True


def _is_clang(cc):
    base = os.path.basename(cc)
    return base.startswith("clang") or base.endswith("clang")


def load_kconfig(srcdir, arch, cc, ld):
    """Parse the Kconfig tree and return the kconfiglib Kconfig object."""
    import kconfiglib

    _install_kconfig_compat()
    srcdir = os.path.abspath(srcdir)
    os.environ["srctree"] = srcdir
    os.environ["ARCH"] = arch
    os.environ["SRCARCH"] = SRCARCH_MAP.get(arch, arch)
    os.environ["CC"] = cc
    os.environ["LD"] = ld
    # Mirror scripts/Makefile.clang so clang's integrated assembler is
    # recognized by scripts/as-version.sh during Kconfig parsing.
    if _is_clang(cc):
        flag = "-fno-integrated-as" if os.environ.get("LLVM_IAS") == "0" \
            else "-fintegrated-as"
        flags = [f for f in os.environ.get("CLANG_FLAGS", "").split() if f]
        if flag not in flags:
            flags.append(flag)
            os.environ["CLANG_FLAGS"] = " ".join(flags)
    return kconfiglib.Kconfig("Kconfig", warn=False), kconfiglib


def parse_config_text(text):
    """Parse .config text -> ordered dict CONFIG_NAME -> raw value string."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(CONFIG_PREFIX) and "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
        elif line.startswith("# " + CONFIG_PREFIX) and line.endswith(
            " is not set"
        ):
            values[line[2 : -len(" is not set")].strip()] = "n"
    return values


def parse_config(path):
    with open(path, encoding="utf-8") as f:
        return parse_config_text(f.read())


def _git_show(spec):
    """Return the content of a git revspec (<rev>:<path>)."""
    try:
        proc = subprocess.run(
            ["git", "show", spec], capture_output=True, text=True
        )
    except FileNotFoundError:
        raise SystemExit(
            "error: git is not installed (needed for git input {!r})".format(spec)
        )
    if proc.returncode != 0:
        raise SystemExit(
            "error: git show {} failed:\n{}".format(spec, proc.stderr.strip())
        )
    return proc.stdout


def _git_commit(rev):
    """Full SHA of the commit a rev points at, or '' if unresolvable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", rev + "^{commit}"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return ""
    if proc.returncode != 0:
        print(
            "warning: could not resolve commit for git rev {!r}".format(rev),
            file=sys.stderr,
        )
        return ""
    return proc.stdout.strip()


def _run_git(git_args, desc):
    """Run a git command in the working tree; return its stdout (or error)."""
    try:
        proc = subprocess.run(["git"] + git_args, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("error: git is not installed (needed for {})".format(desc))
    if proc.returncode != 0:
        raise SystemExit(
            "error: git {} failed:\n{}".format(
                " ".join(git_args), proc.stderr.strip()
            )
        )
    return proc.stdout


def _git_add(paths):
    _run_git(["add", "--"] + list(paths), "add")


def _git_commit_message(message, signoff=False):
    """Commit the staged changes with the given message; return the new SHA."""
    args = ["commit", "-m", message]
    if signoff:
        args.append("-s")
    _run_git(args, "commit")
    return _run_git(["rev-parse", "HEAD"], "rev-parse").strip()


def resolve_config(arg, sibling_path=None):
    """Resolve a config operand to a file path or a git revspec.

    Returns a dict with file (path or None), text (str or None), label
    (display/config path), and commit (full SHA for a git rev, else '').
    An existing path is used as-is; anything else is treated as a git rev
    (rev:path, or a bare rev that borrows sibling_path).
    """
    if os.path.exists(arg):
        return {"file": arg, "text": None, "label": arg, "commit": ""}
    if ":" in arg:
        rev, path = arg.split(":", 1)
    else:
        if sibling_path is None:
            raise SystemExit(
                "error: git ref {!r} needs a path (use <ref>:<path>)".format(arg)
            )
        rev, path = arg, sibling_path
    return {
        "file": None,
        "text": _git_show("{}:{}".format(rev, path)),
        "label": path,
        "commit": _git_commit(rev),
    }


def _raw_config_from(resolved):
    if resolved["file"] is not None:
        return parse_config(resolved["file"])
    return parse_config_text(resolved["text"])


def _source_text(resolved):
    if resolved["file"] is not None:
        with open(resolved["file"], encoding="utf-8") as f:
            return f.read()
    return resolved["text"]


def _config_map_from(resolved, sheet=None):
    if resolved["file"] is not None:
        return load_config_map(resolved["file"], sheet)
    return {
        k: strip_quotes(v) for k, v in parse_config_text(resolved["text"]).items()
    }


def strip_quotes(val):
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1]
    return val


def sym_type(kconfiglib, sym):
    if sym is None:
        return ""
    if sym.choice is not None:
        return "choice"
    return {
        kconfiglib.BOOL: "bool",
        kconfiglib.TRISTATE: "tristate",
        kconfiglib.STRING: "string",
        kconfiglib.INT: "int",
        kconfiglib.HEX: "hex",
    }.get(sym.type, "")


def sym_title(sym):
    if sym is None:
        return ""
    for node in sym.nodes:
        if node.prompt:
            prompt = node.prompt
            return prompt[0] if isinstance(prompt, tuple) else prompt
    return ""


def sym_defaults(kconfiglib, sym):
    if sym is None:
        return ""
    parts = []
    for entry in sym.defaults:
        if len(entry) == 2:
            val, cond = entry
        else:
            val, cond = entry, kconfiglib.y
        if isinstance(val, kconfiglib.Symbol):
            sval = val.str_value if val.is_constant else val.name
        elif isinstance(val, tuple):
            # default value is an expression (e.g. def_bool <expr>)
            sval = kconfiglib.expr_str(val)
        else:
            sval = str(val)
        cstr = kconfiglib.expr_str(cond)
        if cstr != "y":
            sval += " if " + cstr
        parts.append(sval)
    return "; ".join(parts)


def sym_depends(kconfiglib, sym):
    if sym is None or not sym.direct_dep:
        return ""
    return kconfiglib.expr_str(sym.direct_dep)


def norm_name(name):
    name = (name or "").strip()
    if name.startswith(CONFIG_PREFIX):
        name = name[len(CONFIG_PREFIX):]
    return name


def load_memo(path):
    """Read memo CSV (name,note) -> dict name -> note (CONFIG_ prefix stripped)."""
    memos = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    start = 0
    if rows:
        header = [c.strip().lower() for c in rows[0]]
        if "name" in header and "note" in header:
            start = 1
    for row in rows[start:]:
        if len(row) < 2:
            continue
        name = norm_name(row[0])
        if not name:
            continue
        note = row[1].strip()
        if note and name in memos and memos[name] != note:
            print(
                "warning: duplicate memo for {}: {!r} -> {!r}".format(
                    name, memos[name], note
                ),
                file=sys.stderr,
            )
        memos[name] = note
    return memos


def _warn_orphan_memos(memos, config_names):
    """Warn about memo entries whose option is absent from the config."""
    orphans = sorted(n for n in memos if n and n not in config_names)
    if orphans:
        print(
            "warning: {} memo(s) have no matching config option (ignored): {}".format(
                len(orphans), ", ".join(CONFIG_PREFIX + n for n in orphans)
            ),
            file=sys.stderr,
        )


def load_history(path):
    """Read a history CSV -> list of [batch, datetime, sheet, name, diff, note]."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    if "batch" not in header:
        raise SystemExit(
            "error: {} is not a history CSV (no 'batch' column)".format(path)
        )
    idx = {c: header.index(c) for c in HISTORY_COLUMNS if c in header}

    def cell(row, col):
        i = idx.get(col)
        return row[i].strip() if i is not None and i < len(row) else ""

    out = []
    for row in rows[1:]:
        date_s = cell(row, "date")
        try:
            dt = datetime.strptime(date_s, HISTORY_DATE_FMT)
        except ValueError:
            raise SystemExit(
                "error: bad date {!r} in {} (expected {})".format(
                    date_s, path, HISTORY_DATE_FMT
                )
            )
        out.append(
            [
                cell(row, "batch"),
                dt,
                cell(row, "base_commit"),
                cell(row, "new_commit"),
                cell(row, "sheet"),
                cell(row, "name"),
                cell(row, "diff"),
                cell(row, "note"),
            ]
        )
    return out


def append_history_csv(path, rows):
    """Append history rows to a CSV, adding a header if the file is new."""
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if exists and [c.strip().lower() for c in _first_header(path, None)] != HISTORY_COLUMNS:
        raise SystemExit(
            "error: {} has an outdated history schema; regenerate it".format(path)
        )
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        if not exists:
            writer.writerow(HISTORY_COLUMNS)
        for batch, dt, base_commit, new_commit, sheet, name, diff, note in rows:
            writer.writerow(
                [
                    batch,
                    dt.strftime(HISTORY_DATE_FMT),
                    base_commit,
                    new_commit,
                    sheet,
                    name,
                    diff,
                    note,
                ]
            )


def _is_xlsx(path):
    return isinstance(path, str) and path.lower().endswith(".xlsx")


def _import_openpyxl():
    try:
        import openpyxl
    except ImportError:
        raise SystemExit(
            "error: openpyxl is required for .xlsx files (pip install openpyxl)"
        )
    return openpyxl


def _cell_str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _normalize_table(rows):
    """rows: list of string lists, first row the header.

    Returns (COLUMNS, rows reordered/padded to COLUMNS); a table with no
    recognizable header is assumed to already be in COLUMNS order.
    """
    header = [c.strip().lower() for c in rows[0]]
    if "name" not in header:
        return COLUMNS, [r + [""] * (len(COLUMNS) - len(r)) for r in rows]
    idx = {c: header.index(c) for c in COLUMNS if c in header}
    norm = []
    for r in rows[1:]:
        norm.append(
            [r[idx[c]] if c in idx and idx[c] < len(r) else "" for c in COLUMNS]
        )
    return COLUMNS, norm


def _open_xlsx_sheet(path, sheet, read_only):
    openpyxl = _import_openpyxl()
    wb = openpyxl.load_workbook(
        path, data_only=True, read_only=read_only
    )
    if not wb.worksheets:
        raise SystemExit("error: no sheets in {}".format(path))
    if sheet is not None:
        if sheet not in wb.sheetnames:
            raise SystemExit(
                "error: no sheet {!r} in {} (sheets: {})".format(
                    sheet, path, ", ".join(wb.sheetnames)
                )
            )
    ws = wb[sheet] if sheet is not None else wb.worksheets[0]
    return wb, ws


def _first_header(path, sheet):
    """Lowercased first non-empty row of a table file, or [] if empty."""
    if _is_xlsx(path):
        wb, ws = _open_xlsx_sheet(path, sheet, read_only=True)
        try:
            for row in ws.iter_rows(values_only=True):
                if any(v is not None and str(v).strip() for v in row):
                    return [
                        str(v).strip().lower() if v is not None else ""
                        for v in row
                    ]
            return []
        finally:
            wb.close()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if any(c.strip() for c in row):
                return [c.strip().lower() for c in row]
    return []


def read_table(path, sheet=None):
    """Read a CSV or .xlsx table, returning (COLUMNS, rows in COLUMNS order)."""
    if _is_xlsx(path):
        wb, ws = _open_xlsx_sheet(path, sheet, read_only=True)
        try:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [_cell_str(v) for v in row]
                if any(c.strip() for c in cells):
                    rows.append(cells)
        finally:
            wb.close()
        if not rows:
            raise SystemExit("error: empty sheet in {}".format(path))
        return _normalize_table(rows)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise SystemExit("error: empty CSV: {}".format(path))
    return _normalize_table(rows)


def _format_sheet(ws, columns, rows):
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(columns, start=1):
        width = max([len(col)] + [len(str(r[i - 1])) for r in rows])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)


def _write_history_sheet(wb, rows, append):
    """Write history rows to the History sheet, appending or replacing it."""
    if append:
        if not rows:
            return
        if HISTORY_SHEET in wb.sheetnames:
            ws = wb[HISTORY_SHEET]
            for row in rows:
                ws.append(row)
            ws.auto_filter.ref = ws.dimensions
        else:
            ws = wb.create_sheet(title=HISTORY_SHEET)
            ws.append(HISTORY_COLUMNS)
            for row in rows:
                ws.append(row)
            _format_sheet(ws, HISTORY_COLUMNS, rows)
        return
    if HISTORY_SHEET in wb.sheetnames:
        del wb[HISTORY_SHEET]
    if not rows:
        return
    ws = wb.create_sheet(title=HISTORY_SHEET)
    ws.append(HISTORY_COLUMNS)
    for row in rows:
        ws.append(row)
    _format_sheet(ws, HISTORY_COLUMNS, rows)


def _write_table_xlsx(path, rows, sheet, history=None, history_append=True):
    openpyxl = _import_openpyxl()

    if os.path.exists(path):
        wb = openpyxl.load_workbook(path)
        if sheet in wb.sheetnames:
            del wb[sheet]
        try:
            ws = wb.create_sheet(title=sheet)
        except ValueError as e:
            raise SystemExit(
                "error: invalid sheet name {!r}: {}".format(sheet, e)
            )
    else:
        wb = openpyxl.Workbook()
        try:
            ws = wb.active
            ws.title = sheet
        except ValueError as e:
            raise SystemExit(
                "error: invalid sheet name {!r}: {}".format(sheet, e)
            )
    ws.append(COLUMNS)
    for row in rows:
        ws.append(row)
    _format_sheet(ws, COLUMNS, rows)
    if history is not None:
        _write_history_sheet(wb, history, history_append)
    wb.save(path)


def write_table(path, rows, sheet=None, history=None, history_append=True):
    if _is_xlsx(path):
        _write_table_xlsx(path, rows, sheet or DEFAULT_SHEET, history, history_append)
        return
    out = sys.stdout if path is None or path == "-" else open(
        path, "w", newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    finally:
        if out is not sys.stdout:
            out.close()


def load_config_map(path, sheet=None):
    """Read CONFIG_NAME -> value from a .config, or a dump table (.csv/.xlsx)."""
    if _is_xlsx(path) or path.lower().endswith(".csv"):
        header = _first_header(path, sheet)
        for col in ("name", "value"):
            if col not in header:
                raise SystemExit(
                    "error: {} does not look like a dump table (no {!r} column)".format(
                        path, col
                    )
                )
        _, rows = read_table(path, sheet)
        name_i, value_i = COLUMNS.index("name"), COLUMNS.index("value")
        return {r[name_i]: strip_quotes(r[value_i]) for r in rows}
    # .config string values are quoted, table values are not; normalize
    # both to the unquoted form so the two compare identically.
    return {k: strip_quotes(v) for k, v in parse_config(path).items()}


def cmd_dump(args):
    values = _raw_config_from(resolve_config(args.config))
    kconf, kconfiglib = load_kconfig(args.srcdir, args.arch, args.cc, args.ld)
    memos = load_memo(args.memo) if (args.memo and os.path.exists(args.memo)) else {}
    if memos:
        _warn_orphan_memos(memos, {norm_name(n) for n in values})
    is_xlsx = _is_xlsx(args.output)
    if args.history and is_xlsx and (args.sheet or DEFAULT_SHEET) == HISTORY_SHEET:
        raise SystemExit(
            "error: cannot build the {} sheet: the data sheet name "
            "{!r} collides with it".format(HISTORY_SHEET, args.sheet or DEFAULT_SHEET)
        )
    rows = []
    unknown = 0
    for name, raw in values.items():
        sym = kconf.syms.get(norm_name(name))
        if sym is None:
            unknown += 1
        rows.append(
            [
                name,
                sym_type(kconfiglib, sym),
                sym_title(sym),
                strip_quotes(raw),
                "",
                sym_defaults(kconfiglib, sym),
                sym_depends(kconfiglib, sym),
                memos.get(norm_name(name), ""),
            ]
        )
    history = (
        load_history(args.history)
        if (args.history and os.path.exists(args.history) and is_xlsx)
        else None
    )
    write_table(args.output, rows, args.sheet, history, history_append=False)
    print(
        "wrote {} rows to {} ({} not found in Kconfig)".format(
            len(rows), args.output or "stdout", unknown
        ),
        file=sys.stderr,
    )


def diff_values(base, new):
    """Compare two config value maps (as from parse_config).

    Returns (name, old, new, kind) records with kind in
    'changed' / 'added' / 'removed', in base order then new order.
    """
    records = []
    for name, old in base.items():
        if name not in new:
            records.append((name, old, "", "removed"))
        elif new[name] != old:
            records.append((name, old, new[name], "changed"))
    for name, newval in new.items():
        if name not in base:
            records.append((name, "", newval, "added"))
    return records


def diff_cell(kind, old, new):
    """Compact diff annotation for one symbol ('' when unchanged)."""
    if kind == "changed":
        return "{} -> {}".format(strip_quotes(old), strip_quotes(new))
    if kind == "added":
        val = strip_quotes(new)
        return "+{}".format(val) if val else "+"
    if kind == "removed":
        val = strip_quotes(old)
        return "-{}".format(val) if val else "-"
    return ""


def unified_config_patch(old_text, new_text, base_label, new_label):
    """Git-style unified diff of two config texts ('' when identical)."""
    import difflib

    return "".join(
        difflib.unified_diff(
            old_text.splitlines(True),
            new_text.splitlines(True),
            fromfile="a/" + base_label,
            tofile="b/" + new_label,
            n=3,
        )
    )


def _apply_config_delta(
    base, new, base_commit, new_commit, kconf, kconfiglib, memos, sheet, note, now,
    batch_id,
):
    """Build the table rows, change records, counts, and history batch for a
    config delta. base/new map norm_name -> value (quotes stripped). The table
    is a view of the current (new) config: one row per symbol, diff column set
    for this run's changes; removed symbols appear only in the history batch.
    """
    name_i = COLUMNS.index("name")
    value_i = COLUMNS.index("value")
    diff_i = COLUMNS.index("diff")
    type_i = COLUMNS.index("type")
    title_i = COLUMNS.index("title")
    default_i = COLUMNS.index("default")
    depends_i = COLUMNS.index("depends")
    memo_i = COLUMNS.index("memo")

    records = diff_values(base, new)
    diff_lookup = {r[0]: r for r in records if r[3] in ("changed", "added")}

    rows = []
    for name, val in new.items():
        row = [""] * len(COLUMNS)
        row[name_i] = CONFIG_PREFIX + name
        row[value_i] = val
        rec = diff_lookup.get(name)
        row[diff_i] = diff_cell(rec[3], rec[1], rec[2]) if rec else ""
        if kconf is not None:
            sym = kconf.syms.get(name)
            if sym is not None:
                row[type_i] = sym_type(kconfiglib, sym)
                row[title_i] = sym_title(sym)
                row[default_i] = sym_defaults(kconfiglib, sym)
                row[depends_i] = sym_depends(kconfiglib, sym)
        row[memo_i] = memos.get(name, "")
        rows.append(row)

    counts = {"changed": 0, "added": 0, "removed": 0}
    for rec in records:
        counts[rec[3]] += 1

    batch = []
    for rec in records:
        cell = diff_cell(rec[3], rec[1], rec[2])
        if cell:
            batch.append(
                [
                    batch_id,
                    now,
                    base_commit,
                    new_commit,
                    sheet,
                    CONFIG_PREFIX + rec[0],
                    cell,
                    note,
                ]
            )
    return rows, records, counts, batch


def _write_memo_csv(rows, path):
    """Write a memo CSV (name,note) from table rows; return the entry count."""
    memo_i = COLUMNS.index("memo")
    name_i = COLUMNS.index("name")
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["name", "note"])
        for row in rows:
            note = row[memo_i].strip() if memo_i < len(row) else ""
            name = row[name_i].strip() if name_i < len(row) else ""
            if name and note:
                writer.writerow([name, note])
                count += 1
    return count


def _classify_cell(cell):
    """Map a diff cell to 'changed'/'added'/'removed' (or None)."""
    cell = (cell or "").strip()
    if not cell:
        return None
    if cell.startswith("+"):
        return "added"
    if cell.startswith("-"):
        return "removed"
    if " -> " in cell:
        return "changed"
    return None


def group_history(rows):
    """Group history rows by batch id, preserving first-appearance order."""
    groups = []
    index = {}
    for row in rows:
        bid = row[0]
        if bid not in index:
            index[bid] = len(groups)
            groups.append((bid, []))
        groups[index[bid]][1].append(row)
    return groups


def batch_summary(batch_id, rows):
    """Summarize one batch's history rows into a dict."""
    counts = {"changed": 0, "added": 0, "removed": 0}
    note = ""
    for r in rows:
        kind = _classify_cell(r[6])
        if kind:
            counts[kind] += 1
        if not note and r[7]:
            note = r[7]
    return {
        "batch": batch_id,
        "date": rows[0][1],
        "base_commit": rows[0][2],
        "new_commit": rows[0][3],
        "sheet": rows[0][4],
        "note": note,
        "counts": counts,
    }


def cmd_diff(args):
    base_res = resolve_config(
        args.base, sibling_path=(args.new if os.path.exists(args.new) else None)
    )
    new_res = resolve_config(
        args.new, sibling_path=(args.base if os.path.exists(args.base) else None)
    )
    if args.patch:
        for flag, res in (("--base", base_res), ("--new", new_res)):
            p = res["file"]
            if p is not None and (
                p.lower().endswith(".csv") or p.lower().endswith(".xlsx")
            ):
                raise SystemExit(
                    "error: --patch needs .config inputs (table given for {})".format(
                        flag
                    )
                )
        base_text = _source_text(base_res)
        new_text = _source_text(new_res)
        patch = unified_config_patch(
            base_text, new_text, base_res["label"], new_res["label"]
        )
        if patch:
            print(patch, end="", file=sys.stdout)
        counts = {"changed": 0, "added": 0, "removed": 0}
        for rec in diff_values(
            {k: strip_quotes(v) for k, v in parse_config_text(base_text).items()},
            {k: strip_quotes(v) for k, v in parse_config_text(new_text).items()},
        ):
            counts[rec[3]] += 1
        print(
            "{} changed, {} added, {} removed{}".format(
                counts["changed"],
                counts["added"],
                counts["removed"],
                "" if patch else " (no differences)",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1 if patch else 0)
    base = _config_map_from(base_res, args.base_sheet)
    new = _config_map_from(new_res, args.new_sheet)
    titles = {}
    if args.srcdir:
        kconf, _ = load_kconfig(args.srcdir, args.arch, args.cc, args.ld)
        for name in set(base) | set(new):
            sym = kconf.syms.get(norm_name(name))
            if sym is not None:
                titles[name] = sym_title(sym)
    sections = {"changed": [], "added": [], "removed": []}
    for name, old, newval, kind in diff_values(base, new):
        title = titles.get(name, "")
        sections[kind].append(
            "  {}: {}{}".format(
                name,
                diff_cell(kind, old, newval),
                "  ({})".format(title) if title else "",
            )
        )
    for kind in ("changed", "added", "removed"):
        if not sections[kind]:
            continue
        print("{} ({}):".format(kind, len(sections[kind])))
        for line in sections[kind]:
            print(line)
    print(
        "{} changed, {} added, {} removed (base {} symbols, new {} symbols)".format(
            len(sections["changed"]),
            len(sections["added"]),
            len(sections["removed"]),
            len(base),
            len(new),
        ),
        file=sys.stderr,
    )


def cmd_diff_merge(args):
    new_res = resolve_config(
        args.new,
        sibling_path=(
            args.base if args.base and os.path.exists(args.base) else None
        ),
    )
    new = {
        norm_name(k): strip_quotes(v) for k, v in _raw_config_from(new_res).items()
    }
    new_commit = new_res["commit"]
    if args.base:
        base_res = resolve_config(
            args.base,
            sibling_path=(args.new if os.path.exists(args.new) else None),
        )
        base = {
            norm_name(k): strip_quotes(v)
            for k, v in _raw_config_from(base_res).items()
        }
        base_commit = base_res["commit"]
        memos = load_memo(args.memo) if (args.memo and os.path.exists(args.memo)) else {}
    elif args.csv:
        header, trows = read_table(args.csv, args.sheet)
        t_name = header.index("name")
        t_value = header.index("value")
        t_memo = header.index("memo")
        base = {
            norm_name(r[t_name]): strip_quotes(r[t_value])
            for r in trows
            if r[t_name].strip()
        }
        base_commit = ""
        memos = load_memo(args.memo) if (args.memo and os.path.exists(args.memo)) else {
            norm_name(r[t_name]): r[t_memo].strip()
            for r in trows
            if r[t_name].strip() and r[t_memo].strip()
        }
    else:
        base_commit = ""
        raise SystemExit("error: diff-merge needs --base <config> or --csv <table>")

    if memos:
        _warn_orphan_memos(memos, set(new))

    kconf, kconfiglib = (
        load_kconfig(args.srcdir, args.arch, args.cc, args.ld)
        if args.srcdir
        else (None, None)
    )

    out_sheet = args.out_sheet or DEFAULT_SHEET
    batch_id = uuid.uuid4().hex[:12]
    now = datetime.now().replace(second=0, microsecond=0)
    note = args.history_note or ""
    rows, records, counts, batch = _apply_config_delta(
        base, new, base_commit, new_commit, kconf, kconfiglib, memos, out_sheet,
        note, now, batch_id,
    )

    sheet_history = None
    if not args.no_history:
        if args.history and batch:
            append_history_csv(args.history, batch)
        if _is_xlsx(args.output) and batch:
            if out_sheet == HISTORY_SHEET:
                raise SystemExit(
                    "error: cannot record history: the output sheet name "
                    "{!r} collides with the {} sheet".format(
                        out_sheet, HISTORY_SHEET
                    )
                )
            sheet_history = batch
    else:
        batch = None

    write_table(args.output, rows, args.out_sheet, sheet_history, history_append=True)

    sinks = []
    if batch and args.history:
        sinks.append(args.history)
    if sheet_history:
        sinks.append(HISTORY_SHEET)
    suffix = " ({} -> {})".format(len(batch), ", ".join(sinks)) if sinks else ""
    print(
        "{} changed, {} added, {} removed -> {}{}".format(
            counts["changed"],
            counts["added"],
            counts["removed"],
            args.output or "stdout",
            suffix,
        ),
        file=sys.stderr,
    )


def cmd_commit(args):
    memo_i = COLUMNS.index("memo")

    new_res = resolve_config(
        args.new,
        sibling_path=(
            args.base if args.base and os.path.exists(args.base) else None
        ),
    )
    if new_res["file"] is None:
        raise SystemExit(
            "error: commit's --new must be a worktree config file (not a git "
            "rev): {!r}".format(args.new)
        )
    if args.base:
        base_res = resolve_config(
            args.base,
            sibling_path=(args.new if os.path.exists(args.new) else None),
        )
        base = {
            norm_name(k): strip_quotes(v)
            for k, v in _raw_config_from(base_res).items()
        }
        base_commit = base_res["commit"]
    else:
        base = {}
        base_commit = ""
    new = {
        norm_name(k): strip_quotes(v) for k, v in _raw_config_from(new_res).items()
    }
    new_commit = new_res["commit"]

    records = diff_values(base, new)
    if not records:
        print("no changes to commit (config matches base)", file=sys.stderr)
        raise SystemExit(0)

    kconf, kconfiglib = (
        load_kconfig(args.srcdir, args.arch, args.cc, args.ld)
        if args.srcdir
        else (None, None)
    )
    memos = load_memo(args.memo) if (args.memo and os.path.exists(args.memo)) else {}
    if args.xlsx and os.path.exists(args.xlsx):
        # The workbook is the annotator: overlay its memo cells (the most
        # recent) onto the memo CSV so annotations made in Excel are not lost
        # when the view is rebuilt below.
        try:
            header, trows = read_table(args.xlsx, args.sheet)
            m_name = header.index("name")
            m_memo = header.index("memo")
        except (SystemExit, ValueError):
            trows = None
        if trows is not None:
            for r in trows:
                name = norm_name(r[m_name])
                note = r[m_memo].strip()
                if name and note:
                    memos[name] = note
    if memos:
        _warn_orphan_memos(memos, set(new))

    batch_id = uuid.uuid4().hex[:12]
    now = datetime.now().replace(second=0, microsecond=0)
    note = args.note or ""
    rows, records, counts, batch = _apply_config_delta(
        base, new, base_commit, new_commit, kconf, kconfiglib, memos, args.sheet,
        note, now, batch_id,
    )

    config_path = new_res["file"]
    has_memos = any(r[memo_i].strip() for r in rows)
    memo_path = args.memo if (not args.no_memo and has_memos) else None
    history_path = args.history
    xlsx_path = args.xlsx

    stage = [config_path, history_path]
    if memo_path:
        stage.append(memo_path)
    if xlsx_path and args.commit_xlsx:
        stage.append(xlsx_path)

    subject = note if note else "Update {}".format(new_res["label"])
    message = (
        "{}\n\n"
        "kcm-batch: {}\n"
        "kcm-base:  {}\n"
        "kcm-delta: {} changed, {} added, {} removed".format(
            subject,
            batch_id,
            base_commit or "(none)",
            counts["changed"],
            counts["added"],
            counts["removed"],
        )
    )

    if args.dry_run:
        print(
            "dry run: {} changed, {} added, {} removed (batch {})".format(
                counts["changed"],
                counts["added"],
                counts["removed"],
                batch_id,
            ),
            file=sys.stderr,
        )
        print("dry run: would stage: {}".format(", ".join(stage)), file=sys.stderr)
        print("dry run: commit message:\n{}".format(message), file=sys.stderr)
        raise SystemExit(0)

    append_history_csv(history_path, batch)
    if xlsx_path:
        write_table(xlsx_path, rows, args.sheet, batch, history_append=True)
    if memo_path:
        _write_memo_csv(rows, memo_path)

    if args.no_git:
        print(
            "recorded batch {} ({} changed, {} added, {} removed); "
            "no git commit (--no-git)".format(
                batch_id, counts["changed"], counts["added"], counts["removed"]
            ),
            file=sys.stderr,
        )
        return

    _git_add([p for p in stage if os.path.exists(p)])
    sha = _git_commit_message(message, signoff=args.signoff)
    print(
        "committed batch {}: {} changed, {} added, {} removed".format(
            batch_id, counts["changed"], counts["added"], counts["removed"]
        ),
        file=sys.stderr,
    )
    print("  files:  {}".format(", ".join(stage)), file=sys.stderr)
    print("  commit: {}".format(sha), file=sys.stderr)


def cmd_diff_split(args):
    header, rows = read_table(args.csv, args.sheet)
    diff_i = header.index("diff")
    name_i = header.index("name")
    out = sys.stdout if args.output is None or args.output == "-" else open(
        args.output, "w", newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["name", "diff"])
        count = 0
        for row in rows:
            diff = row[diff_i].strip() if diff_i < len(row) else ""
            name = row[name_i].strip() if name_i < len(row) else ""
            if name and diff:
                writer.writerow([name, diff])
                count += 1
    finally:
        if out is not sys.stdout:
            out.close()
    print(
        "split {} diffs from {} rows -> {}".format(
            count, len(rows), args.output or "stdout"
        ),
        file=sys.stderr,
    )


def cmd_memo_merge(args):
    memos = load_memo(args.memo)
    header, rows = read_table(args.csv, args.sheet)
    merged = 0
    for row in rows:
        name = norm_name(row[0])
        note = memos.get(name)
        if note:
            row[header.index("memo")] = note
            merged += 1
    write_table(args.output, rows, args.out_sheet)
    print(
        "merged {} memos into {} rows -> {}".format(
            merged, len(rows), args.output or "stdout"
        ),
        file=sys.stderr,
    )


def cmd_memo_split(args):
    header, rows = read_table(args.csv, args.sheet)
    memo_i = header.index("memo")
    name_i = header.index("name")
    out = sys.stdout if args.output is None or args.output == "-" else open(
        args.output, "w", newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["name", "note"])
        count = 0
        for row in rows:
            note = row[memo_i].strip() if memo_i < len(row) else ""
            name = row[name_i].strip() if name_i < len(row) else ""
            if name and note:
                writer.writerow([name, note])
                count += 1
    finally:
        if out is not sys.stdout:
            out.close()
    print(
        "split {} memos from {} rows -> {}".format(
            count, len(rows), args.output or "stdout"
        ),
        file=sys.stderr,
    )


def cmd_history_split(args):
    if not _is_xlsx(args.csv):
        raise SystemExit(
            "error: history-split reads a .xlsx workbook (got {})".format(args.csv)
        )
    openpyxl = _import_openpyxl()
    wb = openpyxl.load_workbook(args.csv, data_only=True, read_only=True)
    try:
        if HISTORY_SHEET not in wb.sheetnames:
            raise SystemExit(
                "error: no {} sheet in {}".format(HISTORY_SHEET, args.csv)
            )
        ws = wb[HISTORY_SHEET]
        raw = []
        for row in ws.iter_rows(values_only=True):
            if any(v is not None and str(v).strip() for v in row):
                raw.append(list(row))
    finally:
        wb.close()
    if not raw:
        raise SystemExit(
            "error: empty {} sheet in {}".format(HISTORY_SHEET, args.csv)
        )
    header = [c.strip().lower() for c in raw[0]]
    idx = {c: header.index(c) for c in HISTORY_COLUMNS if c in header}
    out = sys.stdout if args.output is None or args.output == "-" else open(
        args.output, "w", newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(HISTORY_COLUMNS)
        count = 0
        for row in raw[1:]:
            vals = []
            for col in HISTORY_COLUMNS:
                i = idx.get(col)
                v = row[i] if i is not None and i < len(row) else None
                if col == "date" and isinstance(v, datetime):
                    vals.append(v.strftime(HISTORY_DATE_FMT))
                else:
                    vals.append(_cell_str(v).strip())
            writer.writerow(vals)
            count += 1
    finally:
        if out is not sys.stdout:
            out.close()
    print(
        "split {} history rows -> {}".format(count, args.output or "stdout"),
        file=sys.stderr,
    )


def cmd_history_log(args):
    if not os.path.exists(args.history):
        raise SystemExit("error: no history file: {}".format(args.history))
    rows = load_history(args.history)
    ordered = group_history(rows)
    if not getattr(args, "reverse", False):
        ordered = list(reversed(ordered))
    limit = getattr(args, "limit", 0)
    if limit:
        ordered = ordered[:limit]

    if getattr(args, "json", False):
        out = []
        for bid, brows in ordered:
            s = batch_summary(bid, brows)
            out.append(
                {
                    "batch": s["batch"],
                    "date": s["date"].strftime(HISTORY_DATE_FMT),
                    "base_commit": s["base_commit"],
                    "new_commit": s["new_commit"],
                    "sheet": s["sheet"],
                    "note": s["note"],
                    "counts": s["counts"],
                }
            )
        print(json.dumps(out, indent=2))
        return

    if getattr(args, "long", False):
        for bid, brows in ordered:
            s = batch_summary(bid, brows)
            c = s["counts"]
            print(s["batch"])
            print("  date   {}".format(s["date"].strftime(HISTORY_DATE_FMT)))
            print("  base   {}".format(s["base_commit"] or "-"))
            print("  new    {}".format(s["new_commit"] or "-"))
            print("  sheet  {}".format(s["sheet"]))
            print(
                "  counts {} changed, {} added, {} removed".format(
                    c["changed"], c["added"], c["removed"]
                )
            )
            print("  note   {}".format(s["note"] or "-"))
            print()
        return

    for bid, brows in ordered:
        s = batch_summary(bid, brows)
        c = s["counts"]
        base = s["base_commit"][:7] if s["base_commit"] else "-"
        new = s["new_commit"][:7] if s["new_commit"] else "-"
        note = "  {}".format(s["note"]) if s["note"] else ""
        print(
            "{}  {}  base={}  new={}  {}c {}a {}r{}".format(
                s["batch"],
                s["date"].strftime(HISTORY_DATE_FMT),
                base,
                new,
                c["changed"],
                c["added"],
                c["removed"],
                note,
            )
        )


def _batch_patch(args, s):
    """Rebuild a git unified diff for one batch from its stored refs."""
    config = getattr(args, "config", None)
    if not config:
        raise SystemExit(
            "error: --patch needs a config path (set 'config' in .kcmrc or "
            "pass --config)"
        )
    base_ref = args.base
    if not base_ref:
        if not s["base_commit"]:
            raise SystemExit("error: --patch needs a base ref (pass --base)")
        base_ref = "{}:{}".format(s["base_commit"], config)
    if args.new:
        new_ref = args.new
    elif s["new_commit"]:
        new_ref = "{}:{}".format(s["new_commit"], config)
    else:
        new_ref = "HEAD:{}".format(config)
    base_res = resolve_config(base_ref, sibling_path=config)
    new_res = resolve_config(new_ref, sibling_path=config)
    return unified_config_patch(
        _source_text(base_res), _source_text(new_res), base_res["label"],
        new_res["label"],
    )


def cmd_history_show(args):
    if not os.path.exists(args.history):
        raise SystemExit("error: no history file: {}".format(args.history))
    rows = load_history(args.history)
    groups = group_history(rows)
    target_bid = None
    target_rows = None
    for bid, brows in groups:
        if bid == args.batch or (
            len(args.batch) >= 4 and bid.startswith(args.batch)
        ):
            target_bid = bid
            target_rows = brows
            break
    if target_rows is None:
        avail = [bid for bid, _ in groups]
        raise SystemExit(
            "error: no batch {!r} in {} ({} batch(es): {})".format(
                args.batch,
                args.history,
                len(avail),
                ", ".join(avail) if avail else "none",
            )
        )
    s = batch_summary(target_bid, target_rows)
    patch = _batch_patch(args, s) if args.patch else None

    if getattr(args, "json", False):
        obj = {
            "batch": s["batch"],
            "date": s["date"].strftime(HISTORY_DATE_FMT),
            "base_commit": s["base_commit"],
            "new_commit": s["new_commit"],
            "sheet": s["sheet"],
            "note": s["note"],
            "counts": s["counts"],
            "changes": [{"name": r[5], "diff": r[6]} for r in target_rows],
        }
        if args.patch:
            obj["patch"] = patch
        print(json.dumps(obj, indent=2))
        return

    c = s["counts"]
    # With --patch the patch is the clean stdout; the human report moves to
    # stderr. Otherwise the report is the stdout output (like `diff`).
    report = sys.stderr if args.patch else sys.stdout
    print("batch   {}".format(s["batch"]), file=report)
    print("date    {}".format(s["date"].strftime(HISTORY_DATE_FMT)), file=report)
    print(
        "base    {}   new    {}".format(
            s["base_commit"] or "-", s["new_commit"] or "-"
        ),
        file=report,
    )
    print(
        "sheet   {}   note   {}".format(s["sheet"], s["note"] or "-"), file=report
    )
    print(
        "counts  {} changed, {} added, {} removed".format(
            c["changed"], c["added"], c["removed"]
        ),
        file=report,
    )
    print(file=report)
    for r in target_rows:
        extra = "  ({})".format(r[7]) if (r[7] and r[7] != s["note"]) else ""
        print("{:<32} {}{}".format(r[5], r[6], extra), file=report)
    if args.patch and patch:
        print(patch, end="")


def _add_sheet_args(p, with_output=False):
    """Add sheet-name flags for .xlsx tables."""
    p.add_argument(
        "--sheet",
        help="sheet in the input workbook (default: first sheet; .xlsx only)",
    )
    if with_output:
        p.add_argument(
            "--out-sheet",
            help="sheet in the output workbook (default: {}; .xlsx only)".format(
                DEFAULT_SHEET
            ),
        )


def _add_kconfig_args(p, srcdir_required=False):
    # srcdir/arch/cc/ld default to None here so .kcmrc can supply them; the
    # built-in defaults are applied in apply_project_defaults().
    p.add_argument(
        "--srcdir",
        default=None,
        help="path to kernel source tree",
    )
    p.add_argument(
        "--arch",
        default=None,
        help="target architecture (default: x86_64)",
    )
    p.add_argument(
        "--cc",
        default=None,
        help="C compiler used for Kconfig checks (default: $CC or gcc)",
    )
    p.add_argument(
        "--ld",
        default=None,
        help="linker used for Kconfig checks (default: $LD or ld)",
    )


def discover_project(cwd):
    """Walk up from cwd looking for .kcmrc; return (path or None, root)."""
    d = os.path.abspath(cwd)
    while True:
        rc = os.path.join(d, RC_NAME)
        if os.path.isfile(rc):
            return rc, d
        parent = os.path.dirname(d)
        if parent == d:
            return None, cwd
        d = parent


def load_kcmrc(path):
    """Read a .kcmrc INI file -> dict of the [kcm] section (lowercased keys)."""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    except (configparser.Error, OSError) as e:
        raise SystemExit("error: bad {} {!r}: {}".format(RC_NAME, path, e))
    if not cp.has_section(RC_SECTION):
        return {}
    return {k: v for k, v in cp[RC_SECTION].items() if v.strip()}


def _rc_bool(v, key):
    """Parse a .kcmrc boolean value. None/empty -> None (unset); else True/False."""
    if v is None or not v.strip():
        return None
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    raise SystemExit(
        "error: bad {} value for '{}': {!r} (expected true/false)".format(
            RC_NAME, key, v
        )
    )


def setup_project(args):
    """Resolve the project (rc + root) and chdir to the root. -> (rc, root).

    --no-rc disables discovery; --rc names a specific file. With no rc the
    root is the CWD and behavior is unchanged.
    """
    cwd = os.getcwd()
    if getattr(args, "no_rc", False):
        return {}, cwd
    rc_path = None
    root = cwd
    if getattr(args, "rc", None):
        p = os.path.abspath(args.rc)
        if not os.path.isfile(p):
            raise SystemExit("error: no such file: {}".format(args.rc))
        rc_path = p
        root = os.path.dirname(p)
    else:
        rc_path, root = discover_project(cwd)
    if rc_path is not None:
        os.chdir(root)
        return load_kcmrc(rc_path), root
    return {}, cwd


def apply_project_defaults(args, rc):
    """Fill unset args from .kcmrc, then apply built-in defaults and check
    required values. Precedence: CLI flag > .kcmrc > built-in."""

    def d(key):
        v = rc.get(key)
        return v if v not in (None, "") else None

    if hasattr(args, "arch"):
        args.arch = args.arch or d("arch") or "x86_64"
    if hasattr(args, "cc"):
        args.cc = args.cc or d("cc") or os.environ.get("CC") or "gcc"
    if hasattr(args, "ld"):
        args.ld = args.ld or d("ld") or os.environ.get("LD") or "ld"
    if hasattr(args, "srcdir"):
        args.srcdir = args.srcdir or d("srcdir")

    cmd = args.command
    cfg = d("config")

    if cmd == "dump":
        args.config = args.config or cfg
        args.sheet = args.sheet or d("sheet")
        args.memo = args.memo or d("memo")
        args.history = args.history or d("history")
        args.output = args.output or d("xlsx")
        if not args.config:
            raise SystemExit(
                "error: dump needs --config (or 'config' in {})".format(RC_NAME)
            )
        if not args.srcdir:
            raise SystemExit(
                "error: dump needs --srcdir (or 'srcdir' in {})".format(RC_NAME)
            )

    elif cmd == "diff":
        args.new = args.new or cfg
        if args.base is None and cfg is not None:
            args.base = "HEAD:{}".format(cfg)
        args.base_sheet = args.base_sheet or d("sheet")
        args.new_sheet = args.new_sheet or d("sheet")
        if not args.base:
            raise SystemExit(
                "error: diff needs --base (or 'config' in {} for "
                "HEAD:config)".format(RC_NAME)
            )
        if not args.new:
            raise SystemExit(
                "error: diff needs --new (or 'config' in {})".format(RC_NAME)
            )

    elif cmd == "diff-merge":
        args.new = args.new or cfg
        if not args.csv and args.base is None and cfg is not None:
            args.base = "HEAD:{}".format(cfg)
        args.sheet = args.sheet or d("sheet")
        args.out_sheet = args.out_sheet or d("sheet")
        args.memo = args.memo or d("memo")
        args.history = args.history or d("history")
        args.output = args.output or d("xlsx")
        if not args.new:
            raise SystemExit(
                "error: diff-merge needs --new (or 'config' in {})".format(RC_NAME)
            )
        if not args.csv and not args.base:
            raise SystemExit(
                "error: diff-merge needs --base (or 'config' in {} for "
                "HEAD:config) or --csv".format(RC_NAME)
            )

    elif cmd in ("diff-split", "memo-merge", "memo-split", "history-split"):
        args.csv = args.csv or d("xlsx")
        if cmd in ("diff-split", "memo-merge", "memo-split"):
            args.sheet = args.sheet or d("sheet")
        if cmd == "memo-merge":
            args.memo = args.memo or d("memo")
        if not args.csv:
            raise SystemExit(
                "error: {} needs --csv (or 'xlsx' in {})".format(cmd, RC_NAME)
            )

    elif cmd == "commit":
        if not args.new:
            args.new = d("config")
        if not args.config:
            args.config = args.new
        if args.base is None and args.config:
            args.base = "HEAD:{}".format(args.config)
        args.sheet = args.sheet or d("sheet") or DEFAULT_SHEET
        args.xlsx = args.xlsx or d("xlsx")
        if args.commit_xlsx is None:
            args.commit_xlsx = _rc_bool(d("commit_xlsx"), "commit_xlsx")
        args.commit_xlsx = bool(args.commit_xlsx)
        args.memo = args.memo or d("memo") or DEFAULT_MEMO
        args.history = args.history or d("history") or DEFAULT_HISTORY
        if not args.new:
            raise SystemExit(
                "error: commit needs --new (or 'config' in {})".format(RC_NAME)
            )

    elif cmd == "history":
        args.history = args.history or d("history") or DEFAULT_HISTORY
        if hasattr(args, "config"):
            args.config = args.config or d("config")


def _add_rc_args(p):
    p.add_argument(
        "--rc",
        help="path to a {} file (default: discovered by walking up from "
        "CWD)".format(RC_NAME),
    )
    p.add_argument(
        "--no-rc", action="store_true", help="do not use a {} file".format(RC_NAME)
    )


def main(argv=None):
    rc_parent = argparse.ArgumentParser(add_help=False)
    _add_rc_args(rc_parent)

    parser = argparse.ArgumentParser(
        prog="kcm",
        description=__doc__.split("\n\n")[0],
        parents=[rc_parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "dump",
        help="dump .config entries to CSV (or .xlsx) with Kconfig metadata",
        parents=[rc_parent],
    )
    p.add_argument(
        "--config",
        default=None,
        help="path to .config (or a git rev <ref>[:<path>]); "
        "defaults to 'config' in .kcmrc",
    )
    _add_kconfig_args(p, srcdir_required=True)
    p.add_argument("--memo", help="memo CSV to pre-fill the memo column")
    p.add_argument(
        "--history",
        help="history CSV to build the {} sheet from (.xlsx only)".format(HISTORY_SHEET),
    )
    p.add_argument(
        "--sheet",
        help="sheet in the output workbook (default: {}; .xlsx only)".format(
            DEFAULT_SHEET
        ),
    )
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser(
        "diff",
        help="human-readable report of the differences between two .config files "
        "(or dump tables)",
        parents=[rc_parent],
    )
    p.add_argument(
        "--base",
        default=None,
        help="original .config (or a git rev <ref>[:<path>]), or a dump table "
        "(.csv/.xlsx); defaults to HEAD:<config>",
    )
    p.add_argument(
        "--base-sheet", help="sheet in the --base workbook (.xlsx only)"
    )
    p.add_argument(
        "--new",
        default=None,
        help="changed .config (or a git rev <ref>[:<path>]), or a dump table "
        "(.csv/.xlsx); defaults to 'config' in .kcmrc",
    )
    p.add_argument(
        "--new-sheet", help="sheet in the --new workbook (.xlsx only)"
    )
    p.add_argument(
        "--patch",
        "-u",
        action="store_true",
        help="emit a git-style unified diff of the two configs to stdout "
        "(needs .config inputs, not tables; exit 1 if there is a diff)",
    )
    _add_kconfig_args(p)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser(
        "diff-merge",
        help="update a dump table against a new config and record the change",
        parents=[rc_parent],
    )
    p.add_argument(
        "--base",
        help="original config (text .config or a git rev <ref>[:<path>]); "
        "pairs with --new for the delta; defaults to HEAD:<config>",
    )
    p.add_argument(
        "--csv",
        help="dump table to use as the base and memo source (or use --base <config>)",
    )
    p.add_argument(
        "--new",
        default=None,
        help="changed config (text .config or a git rev <ref>[:<path>]); "
        "defaults to 'config' in .kcmrc",
    )
    p.add_argument(
        "--memo",
        help="memo CSV to apply to the output (overrides table memos)",
    )
    _add_sheet_args(p, with_output=True)
    _add_kconfig_args(p)
    p.add_argument(
        "--history",
        help="history CSV to append this run's batch to (created if missing)",
    )
    p.add_argument(
        "--no-history",
        action="store_true",
        help="record no history (skip the {} sheet and --history)".format(HISTORY_SHEET),
    )
    p.add_argument(
        "--history-note",
        help="note stamped on every {} entry of this run".format(HISTORY_SHEET),
    )
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_diff_merge)

    p = sub.add_parser(
        "diff-split",
        help="extract the diff column to a diff CSV",
        parents=[rc_parent],
    )
    p.add_argument(
        "--csv",
        default=None,
        help="annotated dump table (.csv or .xlsx); defaults to 'xlsx' in .kcmrc",
    )
    _add_sheet_args(p)
    p.add_argument("-o", "--output", help="output diff CSV (default: stdout)")
    p.set_defaults(func=cmd_diff_split)

    p = sub.add_parser(
        "memo-merge",
        help="fill the memo column of a dump table",
        parents=[rc_parent],
    )
    p.add_argument(
        "--csv",
        default=None,
        help="dump table (.csv or .xlsx); defaults to 'xlsx' in .kcmrc",
    )
    p.add_argument(
        "--memo", default=None, help="memo CSV (name,note); defaults to 'memo' in .kcmrc"
    )
    _add_sheet_args(p, with_output=True)
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_memo_merge)

    p = sub.add_parser(
        "memo-split",
        help="extract the memo column to a memo CSV",
        parents=[rc_parent],
    )
    p.add_argument(
        "--csv",
        default=None,
        help="annotated dump table (.csv or .xlsx); defaults to 'xlsx' in .kcmrc",
    )
    _add_sheet_args(p)
    p.add_argument("-o", "--output", help="output memo CSV (default: stdout)")
    p.set_defaults(func=cmd_memo_split)

    p = sub.add_parser(
        "history-split",
        help="extract the {} sheet of a workbook to a history CSV".format(HISTORY_SHEET),
        parents=[rc_parent],
    )
    p.add_argument(
        "--csv",
        default=None,
        help="workbook (.xlsx) with a {} sheet; defaults to 'xlsx' in "
        ".kcmrc".format(HISTORY_SHEET),
    )
    p.add_argument("-o", "--output", help="output history CSV (default: stdout)")
    p.set_defaults(func=cmd_history_split)

    p = sub.add_parser(
        "commit",
        help="record the committed->working change and git-commit it "
        "(workbook + memos + history + config)",
        parents=[rc_parent],
    )
    p.add_argument(
        "--config",
        help="worktree config file to commit (default: 'config' in .kcmrc, "
        "or --new)",
    )
    p.add_argument(
        "--new",
        help="current config to read (default: 'config' in .kcmrc)",
    )
    p.add_argument(
        "--base",
        help="config to diff against (default: HEAD:<config>)",
    )
    p.add_argument("--note", help="note for this batch / commit subject")
    p.add_argument(
        "--signoff",
        "-s",
        action="store_true",
        help="add a Signed-off-by line to the commit",
    )
    p.add_argument(
        "--xlsx", help="workbook to update (view of the state); written locally"
    )
    p.add_argument(
        "--commit-xlsx",
        action="store_true",
        default=None,
        help="also git-stage/commit the workbook (a derived binary; off by default)",
    )
    p.add_argument(
        "--no-commit-xlsx",
        action="store_false",
        dest="commit_xlsx",
        help="do not git-stage/commit the workbook (overrides 'commit_xlsx' in "
        + RC_NAME,
    )
    p.add_argument(
        "--sheet",
        help="sheet in the output workbook (default: {}; .xlsx only)".format(
            DEFAULT_SHEET
        ),
    )
    p.add_argument("--memo", help="memo CSV to refresh and commit")
    p.add_argument("--history", help="history CSV to append and commit")
    _add_kconfig_args(p)
    p.add_argument(
        "--no-git",
        action="store_true",
        help="write the memos/history/workbook but do not run git",
    )
    p.add_argument(
        "--no-memo",
        action="store_true",
        help="do not refresh the memo CSV",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report the change without writing files or running git",
    )
    p.set_defaults(func=cmd_commit)

    hist_parent = argparse.ArgumentParser(add_help=False)
    hist_parent.add_argument(
        "--history",
        help="history CSV (default: 'history' in .kcmrc or {})".format(
            DEFAULT_HISTORY
        ),
    )
    hist_parent.add_argument(
        "--json", action="store_true", help="emit JSON on stdout"
    )

    p = sub.add_parser(
        "history",
        help="view the change history (log) or one batch (show)",
        parents=[rc_parent, hist_parent],
    )
    p.set_defaults(func=cmd_history_log)
    hsub = p.add_subparsers(dest="history_command")

    pl = hsub.add_parser(
        "log", help="list batches (default for 'history')", parents=[hist_parent]
    )
    pl.add_argument(
        "--limit", type=int, help="show only the N most recent batches"
    )
    pl.add_argument(
        "--reverse",
        action="store_true",
        help="oldest first (default: newest first)",
    )
    pl.add_argument(
        "--long",
        action="store_true",
        help="one block per batch instead of one line each",
    )
    pl.set_defaults(func=cmd_history_log)

    ps = hsub.add_parser(
        "show", help="show one batch's per-symbol changes", parents=[hist_parent]
    )
    ps.add_argument("batch", help="batch id (full or unique prefix)")
    ps.add_argument(
        "--patch",
        action="store_true",
        help="also emit a git unified diff of the batch",
    )
    ps.add_argument(
        "--base",
        help="base ref/path for --patch (default: <base_commit>:<config>)",
    )
    ps.add_argument(
        "--new",
        help="new ref/path for --patch (default: <new_commit>:<config>, "
        "else HEAD:<config>)",
    )
    ps.add_argument(
        "--config",
        help="config path for --patch (default: 'config' in .kcmrc)",
    )
    ps.set_defaults(func=cmd_history_show)

    args = parser.parse_args(argv)
    rc, _root = setup_project(args)
    apply_project_defaults(args, rc)
    args.func(args)


if __name__ == "__main__":
    main()
