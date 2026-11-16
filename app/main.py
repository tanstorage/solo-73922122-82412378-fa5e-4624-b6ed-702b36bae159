"""FastAPI 应用：疾控汇总发布安全审查台。

页面：
- GET /        内部审查台（导入、预览、发布；可见原始格值）
- GET /public  公开页（仅人数与获准汇总，无格内检出数）

接口：
- POST /api/import     导入调查 CSV（仅允许一次，导入后不可更换）
- GET  /api/admin/state 内部状态（含格内检出数）
- POST /api/preview    预览交集检出总数并做安全评估
- POST /api/publish    确认发布（按最新公开记录重核后原子写入）
- GET  /api/public     公开数据（人数 + 获准汇总）
"""

import asyncio
import csv
import io
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .database import (
    get_cells,
    get_publications,
    init_db,
    insert_cells,
    insert_publication,
)
from .solver import evaluate

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLE_CSV = BASE_DIR / "sample_survey.csv"

MAX_CELLS = 16
MIN_POPULATION = 5


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    # 安全评估经 asyncio.to_thread 走默认执行器；扩大线程池，
    # 保证多路慢审查（求解）并行时，预览等请求仍有可用工作线程。
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=16, thread_name_prefix="solver")
    )
    yield


app = FastAPI(title="疾控汇总发布安全审查台", lifespan=lifespan)

# 发布串行化：锁只保护"核对快照未变 + 原子写入"这一小段纯 DB 操作。
# 求解（可能很慢）一律在锁外进行，慢审查不会阻塞其他发布、预览或公开页。
publish_lock = asyncio.Lock()

# 快照过期后在锁外按最新记录重核的重试次数；
# 超过则在锁内做最终重核，保证正确性与终止性（极低概率路径）。
MAX_PUBLISH_RETRIES = 3


# ---------------- CSV 导入 ----------------

_HEADER_ALIASES = {
    "district": {"分区", "district", "地区", "区域"},
    "age_group": {"年龄组", "age_group", "agegroup", "age"},
    "population": {"人数", "population", "pop"},
    "detected": {"检出数", "detected", "count"},
}


def parse_survey_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(str(cell).strip() for cell in r)]
    if not rows:
        raise ValueError("CSV 为空")
    header = [str(h).strip().lstrip("﻿").lower() for h in rows[0]]
    col: dict[str, int] = {}
    for field, aliases in _HEADER_ALIASES.items():
        aliases = {a.lower() for a in aliases}
        for idx, h in enumerate(header):
            if h in aliases:
                col[field] = idx
                break
        if field not in col:
            raise ValueError(f"缺少必需列：{field}（可用表头：{sorted(aliases)}）")

    out, seen = [], set()
    for lineno, r in enumerate(rows[1:], start=2):
        try:
            district = r[col["district"]].strip()
            age = r[col["age_group"]].strip()
            pop_s = r[col["population"]].strip()
            det_s = r[col["detected"]].strip()
        except IndexError:
            raise ValueError(f"第 {lineno} 行列数不足")
        if not district or not age:
            raise ValueError(f"第 {lineno} 行分区或年龄组为空")
        try:
            pop = int(pop_s)
        except ValueError:
            raise ValueError(f"第 {lineno} 行人数不是整数：{pop_s!r}")
        try:
            det = int(det_s)
        except ValueError:
            raise ValueError(f"第 {lineno} 行检出数不是整数：{det_s!r}")
        if pop < MIN_POPULATION:
            raise ValueError(f"第 {lineno} 行人数 {pop} 小于下限 {MIN_POPULATION}")
        if not (0 <= det <= pop):
            raise ValueError(f"第 {lineno} 行检出数 {det} 不在 0~人数({pop}) 范围内")
        key = (district, age)
        if key in seen:
            raise ValueError(f"组合重复：{district} / {age}")
        seen.add(key)
        out.append(
            {"district": district, "age_group": age, "population": pop, "detected": det}
        )
    if not out:
        raise ValueError("没有数据行")
    if len(out) > MAX_CELLS:
        raise ValueError(f"格数 {len(out)} 超过上限 {MAX_CELLS}")
    return out


# ---------------- 请求模型 ----------------


class Selection(BaseModel):
    districts: list[str] = Field(default_factory=list)
    age_groups: list[str] = Field(default_factory=list)
    # 仅用于演示/测试：注入求解耗时（秒），模拟"慢审查"。正常发布留 0。
    demo_delay: float = Field(default=0, ge=0, le=30)


# ---------------- 工具 ----------------


def _match(cells: list[dict], sel: Selection) -> tuple[list[dict], int]:
    known_d = {c["district"] for c in cells}
    known_a = {c["age_group"] for c in cells}
    bad_d = [d for d in sel.districts if d not in known_d]
    bad_a = [a for a in sel.age_groups if a not in known_a]
    if bad_d or bad_a:
        raise HTTPException(
            400, f"未知的分区或年龄组：{bad_d + bad_a}"
        )
    ds, ags = set(sel.districts), set(sel.age_groups)
    matched = [c for c in cells if c["district"] in ds and c["age_group"] in ags]
    if not matched:
        raise HTTPException(400, "选择为空或交集不含任何格")
    return matched, sum(c["detected"] for c in matched)


def _pub_ids(pubs: list[dict]) -> list[int]:
    """公开记录的 id 序列。记录只插入、不更新/删除，序列相同即期间无新增。"""
    return [p["id"] for p in pubs]


def _solve(cells: list[dict], pubs: list[dict], candidate: dict, delay: float = 0.0) -> dict:
    """在工作线程中做安全评估；delay 仅用于演示/测试慢审查。"""
    if delay > 0:
        time.sleep(delay)
    return evaluate(cells, pubs, candidate)


def _reject_unsafe(result: dict) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "message": "按最新公开记录重核未通过：可能暴露小样本格值，已拒绝发布",
            "affected": result["affected"],
        },
    )


# ---------------- 页面 ----------------


@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/public", include_in_schema=False)
def public_page():
    return FileResponse(STATIC_DIR / "public.html")


@app.get("/sample_survey.csv", include_in_schema=False)
def sample_csv():
    return FileResponse(SAMPLE_CSV, media_type="text/csv")


# ---------------- 接口 ----------------


@app.post("/api/import", status_code=201)
async def import_csv(file: UploadFile = File(...)):
    if get_cells():
        raise HTTPException(409, "调查数据已导入，不可更换")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件编码须为 UTF-8")
    try:
        rows = parse_survey_csv(text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        insert_cells(rows)
    except sqlite3.IntegrityError:
        # 并发导入或组合冲突：整体回滚，先到先得。
        raise HTTPException(409, "调查数据已导入，不可更换")
    return {
        "imported": len(rows),
        "districts": sorted({r["district"] for r in rows}),
        "age_groups": sorted({r["age_group"] for r in rows}),
    }


@app.get("/api/admin/state")
def admin_state():
    cells = get_cells()
    return {
        "imported": bool(cells),
        "districts": sorted({c["district"] for c in cells}),
        "age_groups": sorted({c["age_group"] for c in cells}),
        "cells": cells,  # 内部台可见原始检出数
        "publications": get_publications(),
    }


@app.post("/api/preview")
async def preview(sel: Selection):
    cells = get_cells()
    if not cells:
        raise HTTPException(400, "尚未导入调查数据")
    matched, total = _match(cells, sel)
    candidate = {
        "districts": sel.districts,
        "age_groups": sel.age_groups,
        "total": total,
    }
    # 求解放入工作线程，求解期间公开页与其他请求不受影响。
    result = await asyncio.to_thread(evaluate, cells, get_publications(), candidate)
    return {"total": total, "cells_matched": len(matched), **result}


@app.post("/api/publish", status_code=201)
async def publish(sel: Selection):
    cells = get_cells()
    if not cells:
        raise HTTPException(400, "尚未导入调查数据")
    matched, total = _match(cells, sel)
    candidate = {
        "districts": sel.districts,
        "age_groups": sel.age_groups,
        "total": total,
    }

    # 慢审查（求解）在锁外进行：不持有发布锁，
    # 其他窗口的发布、预览与公开页在此期间照常可用。
    pubs_snapshot = get_publications()
    result = await asyncio.to_thread(_solve, cells, pubs_snapshot, candidate, sel.demo_delay)

    for attempt in range(MAX_PUBLISH_RETRIES + 1):
        if not result["safe"]:
            # 不安全是单调的：新增公开记录只会收紧约束，不会由不安全变安全。
            _reject_unsafe(result)
        async with publish_lock:
            pubs_now = get_publications()
            if _pub_ids(pubs_now) == _pub_ids(pubs_snapshot):
                # 快照即最新公开记录：求解结果有效，单事务原子写入。
                try:
                    return insert_publication(sel.districts, sel.age_groups, total)
                except Exception:
                    # 失败即整体回滚，不留半次发布，也不妨碍后续发布。
                    raise HTTPException(500, "保存失败，未产生任何发布记录")
            if attempt == MAX_PUBLISH_RETRIES:
                # 多次重试仍遇并发写入：锁内按最新记录最终重核，保证正确与终止。
                result = await asyncio.to_thread(evaluate, cells, pubs_now, candidate)
                if not result["safe"]:
                    _reject_unsafe(result)
                try:
                    return insert_publication(sel.districts, sel.age_groups, total)
                except Exception:
                    raise HTTPException(500, "保存失败，未产生任何发布记录")
        # 锁外：慢审查恢复后发现期间有新增公开记录——计入新记录重核，
        # 仍安全则放行，不安全才拒绝，而非仅因记录变化就拒绝。
        pubs_snapshot = pubs_now
        result = await asyncio.to_thread(evaluate, cells, pubs_snapshot, candidate)


@app.get("/api/public")
def public_data():
    """公开数据：仅人数与获准汇总，绝不包含格内检出数。"""
    cells = get_cells()
    return {
        "imported": bool(cells),
        "cells": [
            {
                "district": c["district"],
                "age_group": c["age_group"],
                "population": c["population"],
            }
            for c in cells
        ],
        "publications": get_publications(),
    }
