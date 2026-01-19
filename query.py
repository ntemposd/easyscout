#!/usr/bin/env python3
"""
Quick SQLite inspector for `reports` (dev helper).

Usage examples:
  python query.py recent --limit 50
  python query.py find --name "giannis"
  python query.py sentinel
  python query.py duplicates
  python query.py show --id 34
  python query.py sql --query "SELECT id, player, created_at FROM reports WHERE player_norm LIKE '%giannis%'"
  python query.py delete --id 34 --yes   # destructive

Default DB path: $DB_PATH or scout_reports.db
"""
import argparse
import json
import os
import sqlite3
import sys

DB_PATH = os.getenv("DB_PATH", "scout_reports.db")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


def print_table(rows, max_width=80):
    """Print list-of-dict rows as a simple aligned table.
    Truncates long values to `max_width` characters for readability.
    """
    if not rows:
        print("(no rows)")
        return
    cols = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)

    # prepare string rows and compute widths
    str_rows = []
    widths = {c: len(c) for c in cols}
    for r in rows:
        sr = {}
        for c in cols:
            v = r.get(c, "")
            s = "" if v is None else str(v)
            if len(s) > max_width:
                s = s[: max_width - 3] + "..."
            sr[c] = s
            widths[c] = max(widths[c], len(s))
        str_rows.append(sr)

    sep = " | "
    header = sep.join(c.ljust(widths[c]) for c in cols)
    line = "-+-".join("-" * widths[c] for c in cols)
    print(header)
    print(line)
    for sr in str_rows:
        print(sep.join(sr[c].ljust(widths[c]) for c in cols))


def cmd_recent(args):
    sql = "SELECT id, player, queried_player, player_norm, queried_player_norm, created_at, use_web, prompt_version, LENGTH(report_md) AS md_len FROM reports ORDER BY created_at DESC LIMIT ?"
    with connect() as conn:
        cur = conn.execute(sql, (args.limit,))
        rows = cur.fetchall()
    data = rows_to_list(rows)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_table(data)


def cmd_find(args):
    patt = f"%{args.name.lower()}%"
    sql = "SELECT id, player, queried_player, player_norm, queried_player_norm, created_at, use_web, prompt_version, LENGTH(report_md) AS md_len FROM reports WHERE player_norm LIKE ? OR queried_player_norm LIKE ? ORDER BY created_at DESC"
    with connect() as conn:
        cur = conn.execute(sql, (patt, patt))
        rows = cur.fetchall()
    data = rows_to_list(rows)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_table(data)


def cmd_sentinel(args):
    sql = "SELECT id, player, player_norm, created_at, report_md FROM reports WHERE report_md LIKE 'PLAYER_NOT_FOUND:%' ORDER BY created_at DESC"
    with connect() as conn:
        cur = conn.execute(sql)
        rows = cur.fetchall()
    data = rows_to_list(rows)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_table(data)


def cmd_duplicates(args):
    sql = "SELECT player_norm, COUNT(*) AS cnt, MAX(created_at) AS last_created FROM reports GROUP BY player_norm HAVING cnt>1 ORDER BY cnt DESC, last_created DESC"
    with connect() as conn:
        cur = conn.execute(sql)
        rows = cur.fetchall()
    data = rows_to_list(rows)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_table(data)


def cmd_aliases(args):
    sql = "SELECT id, player_norm, queried_player, queried_player_norm, first_seen, last_seen, count FROM player_aliases"
    params = []
    if getattr(args, "player", None):
        patt = f"%{args.player.lower()}%"
        sql += " WHERE player_norm LIKE ? OR queried_player_norm LIKE ?"
        params.extend([patt, patt])
    sql += " ORDER BY last_seen DESC"
    if getattr(args, "limit", None):
        sql += " LIMIT ?"
        params.append(args.limit)
    with connect() as conn:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
    data = rows_to_list(rows)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_table(data)


def cmd_show(args):
    sql = "SELECT * FROM reports WHERE id = ?"
    with connect() as conn:
        cur = conn.execute(sql, (args.id,))
        row = cur.fetchone()
    if not row:
        print(json.dumps({"error": "not found"}, indent=2))
        return
    # convert Row -> dict; ensure report_md included
    data = dict(row)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        # print as key: value rows
        for k, v in data.items():
            if v is None:
                v = ""
            s = str(v)
            if "\n" in s or len(s) > 200:
                s = s[:197] + "..."
            print(f"{k}: {s}")


def cmd_sql(args):
    with connect() as conn:
        cur = conn.execute(args.query)
        rows = cur.fetchall()
    data = rows_to_list(rows)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_table(data)


def cmd_delete(args):
    if not args.yes:
        print("Refusing to delete without --yes. Use --yes to confirm.")
        return
    with connect() as conn:
        cur = conn.execute("DELETE FROM reports WHERE id = ?", (args.id,))
        conn.commit()
        print(json.dumps({"deleted": cur.rowcount}, indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="query.py", description="Inspect scout_reports SQLite DB"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output raw JSON instead of table"
    )
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("recent", help="Show recent reports summary")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("find", help="Find reports by player name (normalized search)")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("sentinel", help="Show any saved PLAYER_NOT_FOUND sentinel rows")
    p.set_defaults(func=cmd_sentinel)

    p = sub.add_parser("duplicates", help="Show duplicate normalized player names")
    p.set_defaults(func=cmd_duplicates)

    p = sub.add_parser("aliases", help="Show player_aliases table")
    p.add_argument("--player", help="Filter by player name fragment")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_aliases)

    p = sub.add_parser("show", help="Show a full report row by id")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("sql", help="Run raw SQL (read-only recommended)")
    p.add_argument("--query", required=True)
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("delete", help="Delete a report row by id (destructive)")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--yes", action="store_true", help="Confirm deletion")
    p.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
