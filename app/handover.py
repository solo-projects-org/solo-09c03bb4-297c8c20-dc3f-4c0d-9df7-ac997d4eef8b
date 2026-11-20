"""按交接编号收表：原录音剪辑表 / 二剪表暂存、逐级换算、整版入库。

- 交接编号（handover_no）唯一。原始剪辑表沿用原 CSV（每行
  `原录音号, 源起点ms, 源终点ms, 成片起点ms`）；二剪表每行
  `上游起点ms, 上游终点ms, 本版起点ms`，并填一个直接上游交接编号。
- 二剪表可先暂存。补交/改交上游后自动重算：上游成片窗口经上游剪辑表
  逐级换算到原录音坐标，跨上游剪接点拆段。
- 只有所有行都完整追溯、且上游链全部入库后，才把整版写入 `versions`
  /`segments`，供禁播登记与遮盖查询使用。入库后该编号不可再改交。
- 缺少上游编号、上游未完整覆盖的行只影响依赖它的表；其他表照常入库。
- 写操作均在 BEGIN IMMEDIATE 事务中完成，交接编号唯一约束保证同一编号
  至多入库一次；重复提交相同内容幂等返回已有表。
"""

from __future__ import annotations

import csv
import io
import sqlite3

from . import logic
from .errors import DomainError
from .intervals import subtract_ranges, union_ranges

RECUT_HEADER = ("upstream_start_ms", "upstream_end_ms", "timeline_start_ms")
MAX_NO = 100


# ---------------------------------------------------------------- 解析

def _parse_recut_csv(text: str) -> list[tuple[int, int, int]]:
    """解析二剪表，返回按本版起点排序的 (上游起点, 上游终点, 本版起点)。"""
    rows: list[tuple[int, int, int]] = []
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    for lineno, raw in enumerate(reader, start=1):
        if not raw or all(not c.strip() for c in raw):
            continue
        cells = [c.strip() for c in raw]
        if lineno == 1 and tuple(cells[:3]) == RECUT_HEADER:
            continue
        if len(cells) != 3:
            raise DomainError(
                422, f"二剪表第 {lineno} 行应有 3 列（上游起点, 上游终点, 本版起点），实际 {len(cells)} 列"
            )
        try:
            up_start, up_end, tl_start = (int(c) for c in cells)
        except ValueError:
            raise DomainError(422, f"二剪表第 {lineno} 行毫秒字段必须是整数")
        if up_start < 0 or tl_start < 0:
            raise DomainError(422, f"二剪表第 {lineno} 行毫秒字段不能为负")
        if up_end <= up_start:
            raise DomainError(422, f"二剪表第 {lineno} 行上游终点必须大于上游起点")
        rows.append((up_start, up_end, tl_start))
    if not rows:
        raise DomainError(422, "二剪表没有有效数据行")
    ordered = sorted(rows, key=lambda r: (r[2], r[0], r[1]))
    prev_end: int | None = None
    for up_start, up_end, tl_start in ordered:
        tl_end = tl_start + (up_end - up_start)
        if prev_end is not None and tl_start < prev_end:
            raise DomainError(422, f"二剪片段在本版成片时间线上重叠：本版起点 {tl_start}ms 早于前一段终点 {prev_end}ms")
        prev_end = tl_end
    return ordered


def _parse_original_csv(text: str) -> list[tuple[str, int, int, int]]:
    """原录音剪辑表沿用原 CSV 语义与校验。"""
    return logic.parse_csv(text)


# ---------------------------------------------------------------- 存取

def _norm_no(value: str, field: str) -> str:
    no = (value or "").strip()
    if not no:
        raise DomainError(422, f"{field}不能为空")
    if len(no) > MAX_NO:
        raise DomainError(422, f"{field}不能超过 {MAX_NO} 个字符")
    return no


def _normalize_upstream(kind: str, upstream_no: str | None) -> str | None:
    if kind not in ("original", "recut"):
        raise DomainError(422, "交接类型必须是 original 或 recut")
    if kind == "original":
        if upstream_no and upstream_no.strip():
            raise DomainError(422, "原录音剪辑表直接取用原录音，不能填上游交接编号")
        return None
    if not upstream_no or not upstream_no.strip():
        raise DomainError(422, "二剪表必须填一个直接上游的交接编号")
    return _norm_no(upstream_no, "上游交接编号")


def _handover_row(conn: sqlite3.Connection, handover_no: str) -> dict | None:
    row = conn.execute(
        "SELECT id, handover_no, name, kind, upstream_no, status, version_id,"
        " created_at, updated_at FROM handovers WHERE handover_no = ?",
        (handover_no,),
    ).fetchone()
    return dict(row) if row else None


def _rows_of(conn: sqlite3.Connection, handover_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT seq, recording_id, src_start_ms, src_end_ms,"
        " upstream_start_ms, upstream_end_ms, tl_start_ms"
        " FROM handover_rows WHERE handover_id = ? ORDER BY seq",
        (handover_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _row_duration(row: dict) -> int:
    if row["recording_id"] is not None:
        return row["src_end_ms"] - row["src_start_ms"]
    return row["upstream_end_ms"] - row["upstream_start_ms"]


def _row_tl_end(row: dict) -> int:
    return row["tl_start_ms"] + _row_duration(row)


def _insert_rows(conn: sqlite3.Connection, handover_id: int, kind: str, parsed: list[tuple]) -> None:
    for seq, rec in enumerate(parsed, start=1):
        if kind == "original":
            recording_id, ss, se, tl = rec
            conn.execute(
                "INSERT INTO handover_rows"
                " (handover_id, seq, recording_id, src_start_ms, src_end_ms, tl_start_ms)"
                " VALUES (?,?,?,?,?,?)",
                (handover_id, seq, recording_id, ss, se, tl),
            )
        else:
            us, ue, tl = rec
            conn.execute(
                "INSERT INTO handover_rows"
                " (handover_id, seq, upstream_start_ms, upstream_end_ms, tl_start_ms)"
                " VALUES (?,?,?,?,?)",
                (handover_id, seq, us, ue, tl),
            )


def _same_content(
    conn: sqlite3.Connection,
    h: dict,
    name: str,
    kind: str,
    upstream_no: str | None,
    parsed: list[tuple],
) -> bool:
    if h["kind"] != kind or h["upstream_no"] != upstream_no or h["name"] != name:
        return False
    stored = []
    for r in _rows_of(conn, h["id"]):
        if h["kind"] == "original":
            stored.append((r["recording_id"], r["src_start_ms"], r["src_end_ms"], r["tl_start_ms"]))
        else:
            stored.append((r["upstream_start_ms"], r["upstream_end_ms"], r["tl_start_ms"]))
    return stored == list(parsed)


# ---------------------------------------------------------------- 追溯

def _would_cycle(conn: sqlite3.Connection, start_no: str | None, target_no: str) -> bool:
    """从 start_no 沿上游链走，若遇到 target_no（或已有环）则说明会形成循环引用。"""
    seen: set[str] = set()
    cur = start_no
    while cur:
        if cur == target_no or cur in seen:
            return True
        seen.add(cur)
        row = _handover_row(conn, cur)
        if row is None:
            return False
        cur = row["upstream_no"] if row["kind"] == "recut" else None
    return False


def _resolve(conn: sqlite3.Connection, h: dict, stack: tuple[str, ...] = ()) -> dict:
    """把交接表换算到原录音坐标。

    返回 pieces（按本版时间线）、每行诊断、缺失的上游编号、是否完整可入库。
    pieces 中每段都对应一段原声，跨上游剪接点时已拆开。
    """
    no = h["handover_no"]
    if no in stack:
        return {"pieces": [], "row_diags": [], "missing_nos": set(), "cycle": True, "complete": False}
    rows = _rows_of(conn, h["id"])

    if h["kind"] == "original":
        pieces: list[dict] = []
        diags: list[dict] = []
        for r in rows:
            piece = {
                "tl_start_ms": r["tl_start_ms"],
                "tl_end_ms": _row_tl_end(r),
                "recording_id": r["recording_id"],
                "src_start_ms": r["src_start_ms"],
                "src_end_ms": r["src_end_ms"],
            }
            pieces.append(piece)
            diags.append({**_diag_base(r), "status": "ok", "missing_ranges": []})
        return {"pieces": pieces, "row_diags": diags, "missing_nos": set(), "cycle": False, "complete": True}

    up = _handover_row(conn, h["upstream_no"])
    if up is None:
        diags = []
        for r in rows:
            diags.append(
                {
                    **_diag_base(r),
                    "status": "missing_upstream",
                    "missing_ranges": [
                        [r["upstream_start_ms"], r["upstream_end_ms"]]
                    ],
                }
            )
        return {
            "pieces": [],
            "row_diags": diags,
            "missing_nos": {h["upstream_no"]},
            "cycle": False,
            "complete": False,
        }

    up_res = _resolve(conn, up, stack + (no,))
    if up_res["cycle"]:
        diags = [
            {**_diag_base(r), "status": "cycle", "missing_ranges": []} for r in rows
        ]
        return {
            "pieces": [],
            "row_diags": diags,
            "missing_nos": set(up_res["missing_nos"]),
            "cycle": True,
            "complete": False,
        }

    up_pieces = up_res["pieces"]
    pieces: list[dict] = []
    diags: list[dict] = []
    for r in rows:
        us = r["upstream_start_ms"]
        ue = r["upstream_end_ms"]
        shift = r["tl_start_ms"] - us
        covered: list[tuple[int, int]] = []
        row_pieces: list[dict] = []
        for p in up_pieces:
            lo = max(us, p["tl_start_ms"])
            hi = min(ue, p["tl_end_ms"])
            if lo < hi:
                covered.append((lo, hi))
                row_pieces.append(
                    {
                        "tl_start_ms": lo + shift,
                        "tl_end_ms": hi + shift,
                        "recording_id": p["recording_id"],
                        "src_start_ms": p["src_start_ms"] + (lo - p["tl_start_ms"]),
                        "src_end_ms": p["src_start_ms"] + (hi - p["tl_start_ms"]),
                    }
                )
        gaps = subtract_ranges([(us, ue)], union_ranges(covered))
        pieces.extend(row_pieces)
        diags.append(
            {
                **_diag_base(r),
                "status": "ok" if not gaps else "uncovered",
                "missing_ranges": [[s, e] for s, e in gaps],
            }
        )

    complete = (
        all(d["status"] == "ok" for d in diags)
        and not up_res["missing_nos"]
        and up_res["complete"]
    )
    return {
        "pieces": pieces,
        "row_diags": diags,
        "missing_nos": set(up_res["missing_nos"]),
        "cycle": False,
        "complete": complete,
    }


def _diag_base(row: dict) -> dict:
    out = {
        "seq": row["seq"],
        "tl_start_ms": row["tl_start_ms"],
        "tl_end_ms": _row_tl_end(row),
    }
    if row["recording_id"] is not None:
        out.update(
            {
                "recording_id": row["recording_id"],
                "src_start_ms": row["src_start_ms"],
                "src_end_ms": row["src_end_ms"],
            }
        )
    else:
        out.update(
            {
                "upstream_start_ms": row["upstream_start_ms"],
                "upstream_end_ms": row["upstream_end_ms"],
            }
        )
    return out


def _waiting_on(conn: sqlite3.Connection, h: dict) -> list[str]:
    """依赖链上尚未入库或缺失的交接编号（由近及远，去重）。"""
    out: list[str] = []
    seen: set[str] = set()
    cur = h
    while cur["kind"] == "recut":
        up_no = cur["upstream_no"]
        if up_no in seen:
            break
        seen.add(up_no)
        up = _handover_row(conn, up_no)
        if up is None:
            out.append(up_no)
            break
        if up["status"] != "admitted":
            out.append(up_no)
        cur = up
    return out


# ---------------------------------------------------------------- 入库

def _admit_pending(conn: sqlite3.Connection) -> None:
    """把依赖齐备、完整追溯的暂存表逐级入库；其余表继续等待。需在写事务内调用。"""
    for _ in range(1000):  # 逐级放行；每轮至少入库一张，上限仅防御异常数据
        progressed = False
        pending = conn.execute(
            "SELECT id, handover_no, name, kind, upstream_no, status, version_id"
            " FROM handovers WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        for hrow in pending:
            h = dict(hrow)
            res = _resolve(conn, h)
            if res["cycle"] or not res["complete"]:
                continue
            if h["kind"] == "recut":
                up = _handover_row(conn, h["upstream_no"])
                if up is None or up["status"] != "admitted":
                    continue
            _admit_one(conn, h, res["pieces"])
            progressed = True
        if not progressed:
            return


def _admit_one(conn: sqlite3.Connection, h: dict, pieces: list[dict]) -> None:
    ordered = sorted(pieces, key=lambda p: (p["tl_start_ms"], p["src_start_ms"]))
    prev_end: int | None = None
    for p in ordered:
        if p["tl_end_ms"] <= p["tl_start_ms"] or (
            prev_end is not None and p["tl_start_ms"] < prev_end
        ):
            raise DomainError(422, f"交接表 {h['handover_no']} 换算后片段在本版时间线上重叠，暂不能入库")
        prev_end = p["tl_end_ms"]

    if not ordered:
        raise DomainError(422, f"交接表 {h['handover_no']} 没有可入库的片段")

    cur = conn.execute("INSERT INTO versions (name) VALUES (?)", (h["name"],))
    vid = cur.lastrowid
    for p in ordered:
        conn.execute(
            "INSERT INTO segments"
            " (version_id, recording_id, src_start_ms, src_end_ms, tl_start_ms, tl_end_ms)"
            " VALUES (?,?,?,?,?,?)",
            (
                vid,
                p["recording_id"],
                p["src_start_ms"],
                p["src_end_ms"],
                p["tl_start_ms"],
                p["tl_end_ms"],
            ),
        )
    upd = conn.execute(
        "UPDATE handovers SET status = 'admitted', version_id = ?,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        " WHERE id = ? AND status = 'pending' AND version_id IS NULL",
        (vid, h["id"]),
    )
    if upd.rowcount != 1:  # 防御：同一编号绝不入库两次
        raise DomainError(409, f"交接表 {h['handover_no']} 已入库，不能重复入库")


# ---------------------------------------------------------------- 对外操作

def create_handover(
    conn: sqlite3.Connection,
    handover_no: str,
    name: str,
    kind: str,
    upstream_no: str | None,
    csv_text: str,
) -> tuple[dict, bool]:
    """收表（可暂存）。返回 (详情, 是否新建)。同编号同内容重送幂等。"""
    no = _norm_no(handover_no, "交接编号")
    name = (name or "").strip()
    if not name:
        raise DomainError(422, "版本名称不能为空")
    upstream = _normalize_upstream(kind, upstream_no)
    parsed = (
        _parse_original_csv(csv_text)
        if kind == "original"
        else _parse_recut_csv(csv_text)
    )
    if _would_cycle(conn, upstream, no):
        raise DomainError(409, f"上游交接编号 {upstream} 与本表形成循环引用，拒收")

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _handover_row(conn, no)
        if existing is not None:
            if _same_content(conn, existing, name, kind, upstream, parsed):
                conn.execute("COMMIT")
                return get_handover(conn, no), False
            if existing["status"] == "admitted":
                raise DomainError(409, f"交接编号 {no} 已入库，不能再改交")
            raise DomainError(
                409, f"交接编号 {no} 已暂存；如需改交请用改交入口更新上游或表内容"
            )
        if _would_cycle(conn, upstream, no):
            raise DomainError(409, f"上游交接编号 {upstream} 与本表形成循环引用，拒收")
        cur = conn.execute(
            "INSERT INTO handovers (handover_no, name, kind, upstream_no, csv_text)"
            " VALUES (?,?,?,?,?)",
            (no, name, kind, upstream, csv_text),
        )
        _insert_rows(conn, cur.lastrowid, kind, parsed)
        _admit_pending(conn)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:  # 并发重送：另一笔已抢先写入同一编号
        conn.execute("ROLLBACK")
        existing = _handover_row(conn, no)
        if existing is not None and _same_content(
            conn, existing, name, kind, upstream, parsed
        ):
            return get_handover(conn, no), False
        raise DomainError(409, f"交接编号 {no} 已被另一笔提交占用") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_handover(conn, no), True


def replace_handover(
    conn: sqlite3.Connection,
    handover_no: str,
    name: str,
    kind: str,
    upstream_no: str | None,
    csv_text: str,
) -> dict:
    """改交尚未入库的表；循环引用则拒收，原表保留。"""
    no = _norm_no(handover_no, "交接编号")
    name = (name or "").strip()
    if not name:
        raise DomainError(422, "版本名称不能为空")
    upstream = _normalize_upstream(kind, upstream_no)
    parsed = (
        _parse_original_csv(csv_text)
        if kind == "original"
        else _parse_recut_csv(csv_text)
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _handover_row(conn, no)
        if existing is None:
            raise DomainError(404, f"交接编号 {no} 不存在")
        if existing["status"] == "admitted":
            raise DomainError(409, f"交接编号 {no} 已入库，不能再改交")
        if existing["kind"] != kind:
            raise DomainError(422, f"交接编号 {no} 的交接类型不可更改")
        if _would_cycle(conn, upstream, no):
            raise DomainError(409, f"上游交接编号 {upstream} 与本表形成循环引用，拒收")
        conn.execute(
            "UPDATE handovers SET name = ?, upstream_no = ?, csv_text = ?,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (name, upstream, csv_text, existing["id"]),
        )
        conn.execute("DELETE FROM handover_rows WHERE handover_id = ?", (existing["id"],))
        _insert_rows(conn, existing["id"], kind, parsed)
        _admit_pending(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_handover(conn, no)


def run_admission_pass(conn: sqlite3.Connection) -> None:
    """重启后接着处理待办：把已齐备的暂存表入库。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _admit_pending(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_handovers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, handover_no, name, kind, upstream_no, status, version_id,"
        " created_at, updated_at FROM handovers ORDER BY id"
    ).fetchall()
    out = []
    for hrow in rows:
        h = dict(hrow)
        res = _resolve(conn, h)
        out.append(
            {
                "handover_no": h["handover_no"],
                "name": h["name"],
                "kind": h["kind"],
                "upstream_no": h["upstream_no"],
                "status": h["status"],
                "version_id": h["version_id"],
                "created_at": h["created_at"],
                "updated_at": h["updated_at"],
                "row_count": len(res["row_diags"]),
                "unresolved_row_count": sum(
                    1 for d in res["row_diags"] if d["status"] != "ok"
                ),
                "missing_upstream_nos": sorted(res["missing_nos"]),
                "cycle_detected": res["cycle"],
                "waiting_on": _waiting_on(conn, h),
            }
        )
    return out


def get_handover(conn: sqlite3.Connection, handover_no: str) -> dict:
    no = _norm_no(handover_no, "交接编号")
    h = _handover_row(conn, no)
    if h is None:
        raise DomainError(404, f"交接编号 {no} 不存在")
    res = _resolve(conn, h)
    csv_row = conn.execute(
        "SELECT csv_text FROM handovers WHERE id = ?", (h["id"],)
    ).fetchone()
    return {
        "handover_no": h["handover_no"],
        "name": h["name"],
        "kind": h["kind"],
        "upstream_no": h["upstream_no"],
        "status": h["status"],
        "version_id": h["version_id"],
        "created_at": h["created_at"],
        "updated_at": h["updated_at"],
        "csv_text": csv_row["csv_text"],
        "row_count": len(res["row_diags"]),
        "rows": res["row_diags"],
        "resolved_segments": sorted(
            res["pieces"], key=lambda p: (p["tl_start_ms"], p["src_start_ms"])
        ),
        "trace_complete": res["complete"],
        "missing_upstream_nos": sorted(res["missing_nos"]),
        "cycle_detected": res["cycle"],
        "waiting_on": _waiting_on(conn, h),
    }
