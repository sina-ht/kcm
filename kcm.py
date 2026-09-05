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

Diff cells (filled by 'diff-merge'): 'old -> new' (value changed), '+v'
(symbol added in the new config), '-v' (symbol removed from it).

Commands:
  dump        .config + Kconfig tree -> CSV
  diff        two .config files -> human-readable change report
  diff-merge  dump CSV + new .config -> CSV with the diff column filled
  diff-split  dump CSV -> diff.csv (name,diff)
  memo-merge  CSV + memo.csv -> CSV with the memo column filled
  memo-split  annotated CSV -> memo.csv (name,note)
"""

import argparse
import csv
import io
import os
import re
import sys

COLUMNS = ["name", "type", "title", "value", "diff", "default", "depends", "memo"]
CONFIG_PREFIX = "CONFIG_"
DEFAULT_SHEET = "config"

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


def parse_config(path):
    """Parse .config -> ordered dict CONFIG_NAME -> raw value string."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
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


def _write_table_xlsx(path, rows, sheet):
    openpyxl = _import_openpyxl()
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

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
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(COLUMNS, start=1):
        width = max([len(col)] + [len(str(r[i - 1])) for r in rows])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)
    wb.save(path)


def write_table(path, rows, sheet=None):
    if _is_xlsx(path):
        _write_table_xlsx(path, rows, sheet or DEFAULT_SHEET)
        return
    out = sys.stdout if path is None or path == "-" else open(
        path, "w", newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(out)
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
    values = parse_config(args.config)
    kconf, kconfiglib = load_kconfig(args.srcdir, args.arch, args.cc, args.ld)
    memos = load_memo(args.memo) if args.memo else {}
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
    write_table(args.output, rows, args.sheet)
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


def cmd_diff(args):
    base = load_config_map(args.base, args.base_sheet)
    new = load_config_map(args.new, args.new_sheet)
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
    new = {norm_name(k): v for k, v in parse_config(args.new).items()}
    header, rows = read_table(args.csv, args.sheet)
    name_i = header.index("name")
    value_i = header.index("value")
    diff_i = header.index("diff")
    counts = {"changed": 0, "added": 0, "removed": 0}
    known = set()
    for row in rows:
        name = norm_name(row[name_i])
        known.add(name)
        old_raw = row[value_i]
        if name not in new:
            row[diff_i] = diff_cell("removed", old_raw, "")
            counts["removed"] += 1
        elif strip_quotes(new[name]) != strip_quotes(old_raw):
            row[diff_i] = diff_cell("changed", old_raw, new[name])
            counts["changed"] += 1
        else:
            row[diff_i] = ""
    kconf, kconfiglib = (
        load_kconfig(args.srcdir, args.arch, args.cc, args.ld)
        if args.srcdir
        else (None, None)
    )
    for name, raw in new.items():
        if name in known:
            continue
        row = [""] * len(COLUMNS)
        row[name_i] = CONFIG_PREFIX + name
        row[value_i] = strip_quotes(raw)
        row[diff_i] = diff_cell("added", "", raw)
        if kconf is not None:
            sym = kconf.syms.get(name)
            if sym is not None:
                row[header.index("type")] = sym_type(kconfiglib, sym)
                row[header.index("title")] = sym_title(sym)
                row[header.index("default")] = sym_defaults(kconfiglib, sym)
                row[header.index("depends")] = sym_depends(kconfiglib, sym)
        rows.append(row)
        counts["added"] += 1
    write_table(args.output, rows, args.out_sheet)
    print(
        "{} changed, {} added, {} removed -> {}".format(
            counts["changed"], counts["added"], counts["removed"],
            args.output or "stdout",
        ),
        file=sys.stderr,
    )


def cmd_diff_split(args):
    header, rows = read_table(args.csv, args.sheet)
    diff_i = header.index("diff")
    name_i = header.index("name")
    out = sys.stdout if args.output is None or args.output == "-" else open(
        args.output, "w", newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(out)
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
        writer = csv.writer(out)
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
    p.add_argument(
        "--srcdir",
        required=srcdir_required,
        help="path to kernel source tree",
    )
    p.add_argument(
        "--arch", default="x86_64", help="target architecture (default: x86_64)"
    )
    p.add_argument(
        "--cc",
        default=os.environ.get("CC", "gcc"),
        help="C compiler used for Kconfig checks (default: $CC or gcc)",
    )
    p.add_argument(
        "--ld",
        default=os.environ.get("LD", "ld"),
        help="linker used for Kconfig checks (default: $LD or ld)",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kcm", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "dump", help="dump .config entries to CSV (or .xlsx) with Kconfig metadata"
    )
    p.add_argument("--config", required=True, help="path to .config")
    _add_kconfig_args(p, srcdir_required=True)
    p.add_argument("--memo", help="memo CSV to pre-fill the memo column")
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
    )
    p.add_argument(
        "--base",
        required=True,
        help="original .config, or a dump table (.csv/.xlsx)",
    )
    p.add_argument(
        "--base-sheet", help="sheet in the --base workbook (.xlsx only)"
    )
    p.add_argument(
        "--new",
        required=True,
        help="changed .config, or a dump table (.csv/.xlsx)",
    )
    p.add_argument(
        "--new-sheet", help="sheet in the --new workbook (.xlsx only)"
    )
    _add_kconfig_args(p)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser(
        "diff-merge",
        help="fill the diff column of a dump table against a new .config",
    )
    p.add_argument("--csv", required=True, help="dump table (.csv or .xlsx, from 'dump')")
    p.add_argument("--new", required=True, help="changed .config")
    _add_sheet_args(p, with_output=True)
    _add_kconfig_args(p)
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_diff_merge)

    p = sub.add_parser("diff-split", help="extract the diff column to a diff CSV")
    p.add_argument("--csv", required=True, help="annotated dump table (.csv or .xlsx)")
    _add_sheet_args(p)
    p.add_argument("-o", "--output", help="output diff CSV (default: stdout)")
    p.set_defaults(func=cmd_diff_split)

    p = sub.add_parser(
        "memo-merge", help="fill the memo column of a dump table"
    )
    p.add_argument("--csv", required=True, help="dump table (.csv or .xlsx)")
    p.add_argument("--memo", required=True, help="memo CSV (name,note)")
    _add_sheet_args(p, with_output=True)
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_memo_merge)

    p = sub.add_parser(
        "memo-split", help="extract the memo column to a memo CSV"
    )
    p.add_argument("--csv", required=True, help="annotated dump table (.csv or .xlsx)")
    _add_sheet_args(p)
    p.add_argument("-o", "--output", help="output memo CSV (default: stdout)")
    p.set_defaults(func=cmd_memo_split)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
