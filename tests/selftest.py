"""关键功能自测：以 TestClient 驱动应用，使用独立临时数据库。

运行：python tests/selftest.py
覆盖：导入校验、导入后不可更换、预览、安全/不安全发布、
两窗口各自安全但合并不安全的重核拒绝、公开页不泄露格内检出数、
公开记录落盘持久化。
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="cdc_guard_test_")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")  # 须在导入 app 前设置

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

SAMPLE = (
    "分区,年龄组,人数,检出数\n"
    "城东,青年,10,2\n"
    "城东,老年,10,8\n"
    "城西,青年,10,8\n"
    "城西,老年,10,8\n"
)

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond and extra:
        print("       " + extra.replace("\n", "\n       ")[:800])


def run():
    with TestClient(app) as client:
        def import_csv(text):
            return client.post(
                "/api/import",
                files={"file": ("data.csv", text.encode("utf-8"), "text/csv")},
            )

        # ---- 1. 导入校验 ----
        r = import_csv("分区,年龄组,人数,检出数\nA,青,4,1\n")
        check("人数<5 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,11\n")
        check("检出数>人数 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,-1\n")
        check("检出数<0 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,1.5\n")
        check("检出数非整数 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,1\nA,青,10,2\n")
        check("组合重复 被拒绝", r.status_code == 400, r.text)
        many = "分区,年龄组,人数,检出数\n" + "\n".join(
            f"区{i},青,10,1" for i in range(17)
        )
        r = import_csv(many)
        check("超过16格 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数\nA,青,10\n")
        check("缺少检出数列 被拒绝", r.status_code == 400, r.text)

        # ---- 2. 正常导入 + 不可更换 ----
        r = import_csv(SAMPLE)
        check("示例导入成功", r.status_code == 201 and r.json()["imported"] == 4, r.text)
        r = import_csv(SAMPLE)
        check("重复导入被拒绝（导入后不可更换）", r.status_code == 409, r.text)

        # ---- 3. 预览 ----
        r = client.post("/api/preview", json={"districts": ["城东"], "age_groups": ["青年", "老年"]})
        j = r.json()
        check("预览「城东」总数=10 且安全",
              r.status_code == 200 and j["total"] == 10 and j["safe"], r.text)

        # ---- 4. 两窗口各自安全、合并不安全 ----
        r = client.post("/api/preview", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        j = r.json()
        check("窗口「老年」单独预览安全（总数=16）",
              r.status_code == 200 and j["total"] == 16 and j["safe"], r.text)

        r = client.post("/api/publish", json={"districts": ["城东"], "age_groups": ["青年", "老年"]})
        check("发布「城东」成功（总数=10）",
              r.status_code == 201 and r.json()["total"] == 10, r.text)

        r = client.post("/api/preview", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        j = r.json()
        aff = j.get("affected", [])
        check("合并后预览「老年」不安全", r.status_code == 200 and not j["safe"], r.text)
        check("受影响格为 城东/青年，可行范围[0,4]，不可取≥5",
              len(aff) == 1
              and aff[0]["district"] == "城东" and aff[0]["age_group"] == "青年"
              and aff[0]["min"] == 0 and aff[0]["max"] == 4
              and aff[0]["zero_feasible"] and not aff[0]["ge5_feasible"], r.text)

        r = client.post("/api/publish", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        check("确认发布「老年」被按最新公开记录重核拒绝(409)", r.status_code == 409, r.text)
        d = r.json().get("detail", {})
        check("409 响应含受影响格与可行范围",
              isinstance(d, dict) and d.get("affected")
              and d["affected"][0]["max"] == 4, r.text)

        # ---- 5. 与已公开记录合并仍安全的发布可继续 ----
        r = client.post("/api/publish", json={"districts": ["城东", "城西"], "age_groups": ["青年"]})
        check("发布「青年」（与「城东」合并仍安全）成功",
              r.status_code == 201 and r.json()["total"] == 10, r.text)
        r = client.post("/api/publish", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        check("「老年」仍被拒绝", r.status_code == 409, r.text)

        # ---- 6. 公开页不泄露格内检出数 ----
        r = client.get("/api/public")
        j = r.json()
        check("公开 API 不含 detected 字段",
              r.status_code == 200 and all("detected" not in c for c in j["cells"]), r.text)
        check("公开 API 含人数与 2 条获准汇总",
              len(j["cells"]) == 4 and all("population" in c for c in j["cells"])
              and len(j["publications"]) == 2, r.text)
        check("公开汇总不含格值字段",
              all(set(p) == {"id", "districts", "age_groups", "total", "created_at"}
                  for p in j["publications"]), r.text)
        r = client.get("/public")
        check("公开页可访问", r.status_code == 200 and "公开" in r.text)
        r = client.get("/")
        check("内部审查台可访问", r.status_code == 200 and "审查台" in r.text)

        # ---- 7. 持久化（直接读库文件，等价于重启后再读） ----
        conn = sqlite3.connect(os.environ["DB_PATH"])
        n_pub = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        n_cell = conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
        conn.close()
        check("公开记录与格值已落盘（重启保留）", n_pub == 2 and n_cell == 4)

        # ---- 8. 其他边界 ----
        r = client.post("/api/preview", json={"districts": ["不存在"], "age_groups": ["青年"]})
        check("未知分区被拒绝", r.status_code == 400, r.text)
        r = client.post("/api/publish", json={"districts": [], "age_groups": ["青年"]})
        check("空选择被拒绝", r.status_code == 400, r.text)
        r = client.post("/api/preview", json={"districts": ["城东"], "age_groups": []})
        check("空年龄组被拒绝", r.status_code == 400, r.text)

        # ---- 9. 保存失败不留半次发布 ----
        import app.database as dbmod
        import app.main as mainmod

        before = len(client.get("/api/public").json()["publications"])

        # 9a. 接口层：插入时发生异常 → 500 且无记录产生
        orig_insert = mainmod.insert_publication
        def _boom(*a, **k):
            raise RuntimeError("模拟保存失败")
        mainmod.insert_publication = _boom
        try:
            r = client.post(
                "/api/publish",
                json={"districts": ["城东", "城西"], "age_groups": ["青年"]},
            )
            check("保存时异常返回 500", r.status_code == 500, r.text)
        finally:
            mainmod.insert_publication = orig_insert
        after = len(client.get("/api/public").json()["publications"])
        check("保存失败未留下半次发布", after == before, f"before={before} after={after}")

        # 9b. 存储层：库不可打开时写入抛错，且库内容不变
        orig_path = dbmod.DB_PATH
        dbmod.DB_PATH = _TMP  # 指向目录：SQLite 无法打开
        try:
            dbmod.insert_publication(["x"], ["y"], 1)
            check("库不可打开时写入抛错", False, "未抛错")
        except Exception:
            check("库不可打开时写入抛错", True)
        finally:
            dbmod.DB_PATH = orig_path
        after = len(client.get("/api/public").json()["publications"])
        check("库写失败后记录数不变", after == before, f"before={before} after={after}")

        # ---- 10. 求解器并发安全（z3 每评估独立 Context） ----
        from concurrent.futures import ThreadPoolExecutor

        from app.solver import evaluate

        cells = dbmod.get_cells()
        pubs = dbmod.get_publications()
        cand = {"districts": ["城东", "城西"], "age_groups": ["老年"], "total": 16}
        with ThreadPoolExecutor(max_workers=8) as ex:
            outs = list(ex.map(lambda _: evaluate(cells, pubs, cand), range(40)))
        check(
            "40 路并发求解结果一致且正确",
            all(
                (not o["safe"])
                and len(o["affected"]) == 1
                and o["affected"][0]["max"] == 4
                for o in outs
            ),
            str(outs[:2]),
        )


run()
print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
