"""核心业务逻辑：CSV 导入、授权选用/换证、编辑确认、授权撤回。

设计要点：
- manifest_items 记录每张（展图 × 原图）**已确认的署名**，而不是写死某个授权号。
  读取时由 EFFECTIVE_ITEMS_SQL 实时解析“当前应选用的授权”：
  同署名的有效商用授权优先（沿用原有选证顺序，即授权 id 最小者），
  没有同署名授权时才退到任一有效商用授权并视为“待确认新署名”。
- 因此撤回一张被上千张展图选用的授权，只需要改 licenses 表的一行；
  manifest_items 不再随展图数量被批量改写。换证后页面、API、确认与下载
  都读取同一个解析结果，同署名的既有确认继续有效。
- 授权一旦撤回即为终态；导入时授权号已存在则整行跳过，重传不能恢复。
"""
import csv
import io
from datetime import datetime


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- CSV 解析

HEADER_ALIASES = {
    "exhibition_no": ["exhibition_no", "exhibition", "exhibition_id", "展图号", "展图编号"],
    "exhibition_title": ["exhibition_title", "title", "展图名称", "展图名"],
    "original_no": ["original_no", "original", "original_id", "原图号", "原图编号"],
    "license_no": ["license_no", "license", "授权号", "授权编号"],
    "commercial": ["commercial", "是否允许商用", "允许商用", "商用"],
    "attribution": ["attribution", "署名", "署名信息"],
}

TRUE_WORDS = {"1", "true", "yes", "y", "是", "允许", "允许商用", "可商用", "商用"}
FALSE_WORDS = {"0", "false", "no", "n", "否", "不允许", "禁止"}


def _read_table(content: bytes):
    text = content.decode("utf-8-sig")  # 兼容 Excel 导出的 BOM
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("CSV 内容为空")
    header = [h.strip() for h in rows[0]]
    return header, rows[1:]


def _column_map(header, required):
    lower = [h.lower() for h in header]
    mapping = {}
    for canon, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower:
                mapping[canon] = lower.index(alias.lower())
                break
    missing = [k for k in required if k not in mapping]
    if missing:
        raise ValueError("CSV 缺少必需列: " + ", ".join(missing))
    return mapping


def _cell(row, idx):
    return row[idx].strip() if idx < len(row) else ""


def parse_commercial(s: str) -> int:
    v = s.strip().lower()
    if v in TRUE_WORDS:
        return 1
    if v in FALSE_WORDS:
        return 0
    raise ValueError(f"无法识别的“是否允许商用”取值: {s!r}")


# ---------------------------------------------------------------- 基础工具

def _get_or_create_original(conn, original_no: str) -> int:
    row = conn.execute("SELECT id FROM originals WHERE original_no=?", (original_no,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO originals (original_no) VALUES (?)", (original_no,)).lastrowid


def _get_or_create_exhibition(conn, exhibition_no: str, title: str = "") -> int:
    row = conn.execute("SELECT id, title FROM exhibitions WHERE exhibition_no=?", (exhibition_no,)).fetchone()
    if row:
        if title and title != row["title"]:
            conn.execute("UPDATE exhibitions SET title=? WHERE id=?", (title, row["id"]))
        return row["id"]
    return conn.execute(
        "INSERT INTO exhibitions (exhibition_no, title) VALUES (?,?)",
        (exhibition_no, title or exhibition_no),
    ).lastrowid


# ---------------------------------------------------------------- 导入

def import_mapping(conn, content: bytes) -> dict:
    """导入展图-原图对照 CSV。幂等：已存在的对照关系不会重复。"""
    header, rows = _read_table(content)
    cols = _column_map(header, ["exhibition_no", "original_no"])
    stats = {"rows": 0, "links_added": 0, "errors": []}
    for i, row in enumerate(rows, start=2):
        try:
            ex_no = _cell(row, cols["exhibition_no"])
            orig_no = _cell(row, cols["original_no"])
            title = _cell(row, cols["exhibition_title"]) if "exhibition_title" in cols else ""
            if not ex_no or not orig_no:
                raise ValueError("展图号或原图号为空")
            ex_id = _get_or_create_exhibition(conn, ex_no, title)
            orig_id = _get_or_create_original(conn, orig_no)
            cur = conn.execute(
                "INSERT OR IGNORE INTO exhibition_originals (exhibition_id, original_id) VALUES (?,?)",
                (ex_id, orig_id),
            )
            stats["links_added"] += cur.rowcount
            stats["rows"] += 1
        except Exception as e:  # 单行失败不影响其他行
            stats["errors"].append(f"第{i}行: {e}")
    return stats


def import_licenses(conn, content: bytes, batch_label: str = "") -> dict:
    """分批导入授权 CSV。

    授权号已存在的行直接跳过：既不覆盖已有数据，也绝不恢复已撤回的授权。
    """
    header, rows = _read_table(content)
    cols = _column_map(header, ["license_no", "original_no", "commercial", "attribution"])
    stats = {"added": 0, "duplicates": 0, "errors": []}
    for i, row in enumerate(rows, start=2):
        try:
            lic_no = _cell(row, cols["license_no"])
            orig_no = _cell(row, cols["original_no"])
            commercial = parse_commercial(_cell(row, cols["commercial"]))
            attribution = _cell(row, cols["attribution"])
            if not lic_no or not orig_no:
                raise ValueError("授权号或原图号为空")
            if not attribution:
                raise ValueError("署名为空")
            orig_id = _get_or_create_original(conn, orig_no)
            exists = conn.execute("SELECT id FROM licenses WHERE license_no=?", (lic_no,)).fetchone()
            if exists:
                stats["duplicates"] += 1  # 重传不生效，撤回状态保持不变
                continue
            conn.execute(
                "INSERT INTO licenses (license_no, original_id, commercial, attribution, batch_label)"
                " VALUES (?,?,?,?,?)",
                (lic_no, orig_id, commercial, attribution, batch_label),
            )
            stats["added"] += 1
        except Exception as e:
            stats["errors"].append(f"第{i}行: {e}")
    return stats


# ---------------------------------------------------------------- 授权选用

# 每张（展图 × 原图）当前应选用的授权，由数据库实时解析，不落库：
#   1. 优先选同一原图下**同署名**的有效商用授权（沿用原有选证顺序：授权 id 最小者）；
#      授权号为追加分配、不会出现更小的新 id，因此同署名换证结果稳定；
#   2. 没有同署名备选时，退到任一有效商用授权（id 最小），并视为“待确认新署名”；
#   3. 完全没有有效商用授权 → license_id 为 NULL，即“缺授权”。
# confirmed 当且仅当已确认的署名正是当前选用授权的署名；同署名换证时确认保持，
# 因此“已有署名确认继续有效”。撤回只需把 licenses 表的一行置为 revoked，
# 无需按展图数量批量改写清单，页面、API、确认与下载在读取时得到同一个授权号。
EFFECTIVE_ITEMS_SQL = """
SELECT eo.exhibition_id,
       e.exhibition_no,
       eo.original_id,
       o.original_no,
       mi.attribution AS confirmed_attribution,
       CASE WHEN mi.confirmed = 1 AND lic.attribution = mi.attribution
            THEN 1 ELSE 0 END AS confirmed,
       lic.id AS license_id,
       lic.license_no,
       lic.commercial,
       lic.revoked,
       lic.attribution AS license_attribution
FROM exhibition_originals eo
JOIN exhibitions e ON e.id = eo.exhibition_id
JOIN originals o ON o.id = eo.original_id
LEFT JOIN manifest_items mi
       ON mi.exhibition_id = eo.exhibition_id AND mi.original_id = eo.original_id
LEFT JOIN licenses same ON same.id = (
    SELECT l.id FROM licenses l
     WHERE l.original_id = eo.original_id AND l.commercial = 1 AND l.revoked = 0
       AND mi.attribution <> '' AND l.attribution = mi.attribution
     ORDER BY l.id LIMIT 1)
LEFT JOIN licenses lic ON lic.id = COALESCE(same.id, (
    SELECT l.id FROM licenses l
     WHERE l.original_id = eo.original_id AND l.commercial = 1 AND l.revoked = 0
     ORDER BY l.id LIMIT 1))
"""


def _effective_rows(conn, where: str = "", params=()) -> list:
    sql = EFFECTIVE_ITEMS_SQL
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY e.exhibition_no, o.original_no"
    return conn.execute(sql, params).fetchall()


def _item_dict(row) -> dict:
    d = dict(row)
    d["confirmed"] = bool(d["confirmed"])
    d["license_valid"] = d["license_id"] is not None
    # 缺授权时仍显示最后一次确认过的署名，便于编辑判断；授权号为空。
    d["attribution"] = d["license_attribution"] if d["license_valid"] else (
        d["confirmed_attribution"] or "")
    d["commercial"] = d["commercial"] if d["license_valid"] else None
    d["revoked"] = d["revoked"] if d["license_valid"] else None
    return d


def get_items(conn, exhibition_id: int) -> list:
    return [_item_dict(r) for r in _effective_rows(
        conn, "eo.exhibition_id=?", (exhibition_id,))]


def exhibition_status(conn, exhibition_id: int) -> dict:
    """汇总：downloadable 当且仅当每张原图都有有效商用授权且署名已确认。

    只读，不写数据库；状态完全由当前授权表与已确认署名解析而来。
    """
    return _status_of(get_items(conn, exhibition_id))


def exhibition_statuses(conn) -> dict:
    """一次查询得到所有展图状态：{exhibition_id: status}，供列表页/列表 API 使用。"""
    grouped = {}
    for r in _effective_rows(conn):
        grouped.setdefault(r["exhibition_id"], []).append(_item_dict(r))
    return {ex_id: _status_of(items) for ex_id, items in grouped.items()}


def empty_status() -> dict:
    return _status_of([])


def _status_of(items: list) -> dict:
    missing = [it["original_no"] for it in items if not it["license_valid"]]
    unconfirmed = [it["original_no"] for it in items
                   if it["license_valid"] and not it["confirmed"]]
    return {
        "items": items,
        "missing": missing,
        "unconfirmed": unconfirmed,
        "downloadable": bool(items) and not missing and not unconfirmed,
    }


def license_usage(conn) -> dict:
    """当前授权使用情况：{license_id: [展图号, ...]}，按实时选用结果统计。"""
    usage = {}
    for r in _effective_rows(conn):
        if r["license_id"] is not None:
            usage.setdefault(r["license_id"], []).append(r["exhibition_no"])
    return usage


def revoke_previews(conn) -> dict:
    """一次查询预演所有在用授权撤回后的结果，{license_id: [预览...]}。

    铺开规则与 revoke_license 完全一致：同署名有效商用授权优先（id 最小），
    否则退到任一有效商用授权（待确认），都没有则缺授权。
    每条预览：{exhibition_no, next_license_no, next_attribution, same_attribution}。
    """
    rows = conn.execute(
        "WITH eff AS (" + EFFECTIVE_ITEMS_SQL + ")"
        " SELECT eff.license_id, eff.exhibition_no, eff.confirmed_attribution,"
        "        COALESCE("
        "          (SELECT l.license_no FROM licenses l"
        "            WHERE l.original_id = eff.original_id AND l.commercial = 1"
        "              AND l.revoked = 0 AND l.id <> eff.license_id"
        "              AND eff.confirmed_attribution <> ''"
        "              AND l.attribution = eff.confirmed_attribution"
        "            ORDER BY l.id LIMIT 1),"
        "          (SELECT l.license_no FROM licenses l"
        "            WHERE l.original_id = eff.original_id AND l.commercial = 1"
        "              AND l.revoked = 0 AND l.id <> eff.license_id"
        "            ORDER BY l.id LIMIT 1)) AS next_license_no,"
        "        COALESCE("
        "          (SELECT l.attribution FROM licenses l"
        "            WHERE l.original_id = eff.original_id AND l.commercial = 1"
        "              AND l.revoked = 0 AND l.id <> eff.license_id"
        "              AND eff.confirmed_attribution <> ''"
        "              AND l.attribution = eff.confirmed_attribution"
        "            ORDER BY l.id LIMIT 1),"
        "          (SELECT l.attribution FROM licenses l"
        "            WHERE l.original_id = eff.original_id AND l.commercial = 1"
        "              AND l.revoked = 0 AND l.id <> eff.license_id"
        "            ORDER BY l.id LIMIT 1)) AS next_attribution"
        " FROM eff WHERE eff.license_id IS NOT NULL"
        " ORDER BY eff.exhibition_no"
    ).fetchall()
    previews = {}
    for r in rows:
        previews.setdefault(r["license_id"], []).append({
            "exhibition_no": r["exhibition_no"],
            "next_license_no": r["next_license_no"],
            "next_attribution": r["next_attribution"],
            "same_attribution": (r["next_license_no"] is not None
                                 and r["next_attribution"] == (r["confirmed_attribution"] or "")),
        })
    return previews


def revoke_preview(conn, license_id: int) -> list:
    """单个授权的撤回预演（只读），规则同 revoke_previews。"""
    return revoke_previews(conn).get(license_id, [])


# ---------------------------------------------------------------- 撤回与确认

def _classify_after_revoke(conn, original_id: int, exhibition_ids) -> dict:
    """撤回后按实时解析结果归类受影响的展图：自动换用 / 待确认新署名 / 缺授权。"""
    result = {"switched": [], "need_confirm": [], "missing": [], "new_licenses": set()}
    wanted = set(exhibition_ids)
    if not wanted:
        return result
    # 该原图的展图通常不多；这里只读，且不拼 IN 列表，避免超长 SQL。
    for r in _effective_rows(conn, "eo.original_id=?", (original_id,)):
        if r["exhibition_id"] not in wanted:
            continue
        if r["license_id"] is None:
            result["missing"].append(r["exhibition_no"])
        elif r["confirmed"]:
            result["switched"].append(r["exhibition_no"])
            result["new_licenses"].add(r["license_no"])
        else:
            result["need_confirm"].append(r["exhibition_no"])
            result["new_licenses"].add(r["license_no"])
    return result


def _names(names, limit: int = 5) -> str:
    names = sorted(names)
    head = "、".join(names[:limit])
    if len(names) > limit:
        return f"{head} 等 {len(names)} 个展图"
    return head


def revoke_license(conn, license_id: int):
    """撤回授权（终态）并让递补立即生效。返回 (license_row, message)。

    撤回与同署名递补在调用方的同一事务内完成：只改 licenses 表的一行，
    实际改动的行数与原图被多少张展图选用无关。若事务失败，调用方回滚，
    原授权继续有效（不会出现“撤回了但没有递补”的中间状态）。
    """
    lic = conn.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
    if lic is None:
        return None, "授权不存在"
    if lic["revoked"]:
        return lic, f"授权 {lic['license_no']} 此前已撤回，重复撤回或重传 CSV 都不会恢复"

    # 撤回前记录真正在用这张授权的展图，仅用于生成结果提示（只读）。
    in_use = [
        r["exhibition_id"] for r in _effective_rows(
            conn, "eo.original_id=? AND lic.id=?", (lic["original_id"], license_id))
    ]

    conn.execute("UPDATE licenses SET revoked=1, revoked_at=? WHERE id=?", (now(), license_id))

    effect = _classify_after_revoke(conn, lic["original_id"], in_use)
    parts = [f"授权 {lic['license_no']} 已撤回"]
    if not in_use:
        parts.append("当前没有展图选用该授权")
    if effect["switched"]:
        parts.append(
            f"同署名递补为 {('、'.join(sorted(effect['new_licenses'])))}："
            f"{_names(effect['switched'])}，原署名确认继续有效")
    if effect["need_confirm"]:
        parts.append(
            f"停供待确认新署名（{('、'.join(sorted(effect['new_licenses'])))}）："
            f"{_names(effect['need_confirm'])}")
    if effect["missing"]:
        parts.append(f"停供缺有效商用授权：{_names(effect['missing'])}")
    return lic, "；".join(parts)


def confirm_manifest(conn, exhibition_id: int, editor: str, submitted: dict) -> list:
    """编辑确认当前清单。submitted: {original_id: (license_no, attribution)}。

    在调用方的事务内执行；返回问题列表，非空表示维持停供（调用方应回滚）。
    确认对象与下载使用同一个实时解析出的授权号；交错场景下若清单中的授权
    刚被撤回或清单已变更，则拒绝确认并提示重新核对。
    同署名递补已由系统自动完成，无需编辑再次确认。
    """
    items = get_items(conn, exhibition_id)
    if not items:
        return ["该展图没有关联原图，无法确认"]
    problems = []
    for it in items:
        if not it["license_valid"]:
            problems.append(
                f"原图 {it['original_no']}：清单中的授权已被撤回或失效，请重新核对"
            )
    for it in items:
        if not it["license_valid"] or it["confirmed"]:
            continue
        sub = submitted.get(it["original_id"])
        if sub is None or sub[0] != (it["license_no"] or "") or sub[1] != it["attribution"]:
            problems.append(f"原图 {it['original_no']}：清单已变更，请重新核对后再确认")
    if problems:
        return problems
    # 只写需要变更的行：已确认且署名未变的清单项不必重写。
    for it in items:
        if it["confirmed"]:
            continue
        conn.execute(
            "INSERT INTO manifest_items (exhibition_id, original_id, attribution, confirmed,"
            " updated_at) VALUES (?,?,?,1,?)"
            " ON CONFLICT(exhibition_id, original_id) DO UPDATE SET"
            " attribution=excluded.attribution, confirmed=1, updated_at=excluded.updated_at",
            (exhibition_id, it["original_id"], it["attribution"], now()),
        )
    detail = "; ".join(f"{it['original_no']}={it['license_no']}" for it in items)
    conn.execute(
        "INSERT INTO confirmations (exhibition_id, editor, detail) VALUES (?,?,?)",
        (exhibition_id, editor, detail),
    )
    return []
