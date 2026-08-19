#!/usr/bin/env python3
# kcm.py -- Linux kernel configuration management tool.
# (C)Copyright 2026 by Hiroshi Takekawa
#
# SPDX-License-Identifier: GPL-2.0-only
"""kcm - Linux kernel configuration management tool.

Dumps .config entries (name, type, title, value, default, depends) to
CSV for viewing/editing in spreadsheet apps, and manages per-config
memos (annotations) in a separate persistent CSV.

Commands:
  dump        .config + Kconfig tree -> CSV
  memo-merge  CSV + memo.csv -> CSV with the memo column filled
  memo-split  annotated CSV -> memo.csv (name,note)
"""

import argparse
import csv
import io
import os
import re
import sys

COLUMNS = ["name", "type", "title", "value", "default", "depends", "memo"]
CONFIG_PREFIX = "CONFIG_"

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


def read_table(path):
    """Read a CSV table, returning (header, rows) with at least COLUMNS present."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise SystemExit("error: empty CSV: {}".format(path))
    header = [c.strip().lower() for c in rows[0]]
    missing = [c for c in COLUMNS if c not in header]
    if missing and "name" not in header:
        # No recognizable header: assume COLUMNS order
        return COLUMNS, [r + [""] * (len(COLUMNS) - len(r)) for r in rows]
    idx = {c: header.index(c) for c in COLUMNS if c in header}
    norm = []
    for r in rows[1:]:
        norm.append(
            [r[idx[c]] if c in idx and idx[c] < len(r) else "" for c in COLUMNS]
        )
    return COLUMNS, norm


def write_table(path, rows):
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
                sym_defaults(kconfiglib, sym),
                sym_depends(kconfiglib, sym),
                memos.get(norm_name(name), ""),
            ]
        )
    write_table(args.output, rows)
    print(
        "wrote {} rows to {} ({} not found in Kconfig)".format(
            len(rows), args.output or "stdout", unknown
        ),
        file=sys.stderr,
    )


def cmd_memo_merge(args):
    memos = load_memo(args.memo)
    header, rows = read_table(args.csv)
    merged = 0
    for row in rows:
        name = norm_name(row[0])
        note = memos.get(name)
        if note:
            row[header.index("memo")] = note
            merged += 1
    write_table(args.output, rows)
    print(
        "merged {} memos into {} rows -> {}".format(
            merged, len(rows), args.output or "stdout"
        ),
        file=sys.stderr,
    )


def cmd_memo_split(args):
    header, rows = read_table(args.csv)
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kcm", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "dump", help="dump .config entries to CSV with Kconfig metadata"
    )
    p.add_argument("--config", required=True, help="path to .config")
    p.add_argument("--srcdir", required=True, help="path to kernel source tree")
    p.add_argument("--arch", default="x86_64", help="target architecture (default: x86_64)")
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
    p.add_argument("--memo", help="memo CSV to pre-fill the memo column")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("memo-merge", help="fill the memo column of a dump CSV")
    p.add_argument("--csv", required=True, help="dump CSV (from 'dump')")
    p.add_argument("--memo", required=True, help="memo CSV (name,note)")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.set_defaults(func=cmd_memo_merge)

    p = sub.add_parser("memo-split", help="extract the memo column to a memo CSV")
    p.add_argument("--csv", required=True, help="annotated dump CSV")
    p.add_argument("-o", "--output", help="output memo CSV (default: stdout)")
    p.set_defaults(func=cmd_memo_split)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
