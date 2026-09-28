"""CDE public-page collector. No private endpoints or CAPTCHA bypass."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

BASE = "https://www.chinadrugtrials.org.cn"
CTR = re.compile(r"CTR\d{8}")
ROOT = Path(__file__).resolve().parent


class WebsiteAccessError(RuntimeError):
    """Public search page could not be reached; stop instead of retrying every keyword."""


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(text):
    return re.sub(r"[ \t\xa0]+", " ", text).strip()


def parse_detail(html, expected_ctr):
    """Parse labels rather than table offsets. Fail closed on the wrong record."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        for i, cell in enumerate(cells[:-1]):
            key = clean(cell.get_text(" ", strip=True)).rstrip(":：")
            if key in {"登记号", "试验专业题目", "试验通俗题目", "药物名称", "适应症",
                       "试验分期", "入选标准", "排除标准", "联系人姓名", "联系人座机",
                       "联系人手机号", "联系人Email", "首次公示信息日期", "健康受试者"}:
                value_cell = cells[i + 1]
                nested = value_cell.find("table")
                if key in {"入选标准", "排除标准"} and nested:
                    items = []
                    for subrow in nested.find_all("tr"):
                        subcells = subrow.find_all(["td", "th"], recursive=False)
                        if len(subcells) >= 2:
                            items.append(clean(subcells[-1].get_text(" ", strip=True)))
                    fields[key] = items
                else:
                    fields.setdefault(key, clean(value_cell.get_text(" ", strip=True)))
    if fields.get("登记号") != expected_ctr:
        raise ValueError(f"详情登记号不匹配：预期 {expected_ctr}，实际 {fields.get('登记号')}")
    if not fields.get("试验专业题目") and not fields.get("试验通俗题目"):
        raise ValueError("详情未完整加载：缺少项目名")
    for key in ("入选标准", "排除标准"):
        if not isinstance(fields.get(key), list) or not fields[key]:
            raise ValueError(f"详情未完整加载：缺少{key}，不会标记为采集成功")
    body = soup.get_text("\n", strip=True)
    pis = []
    # PI tables use exact labels 姓名/单位名称. Keep empty cells so a missing
    # telephone is never mistaken for the following Email label.
    for table in soup.find_all("table"):
        rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
        matrix = [[clean(c.get_text(" ", strip=True)) for c in r.find_all(["td", "th"], recursive=False)] for r in rows]
        labels = {c for row in matrix for c in row}
        if not {"姓名", "单位名称"}.issubset(labels):
            continue
        current = {}
        for row in matrix:
            for i, token in enumerate(row[:-1]):
                if token == "姓名":
                    if current:
                        pis.append(current)
                    current = {"姓名": row[i + 1] or "页面未公开"}
                elif token in {"电话", "Email", "单位名称"} and current:
                    current[token] = row[i + 1] or "页面未公开"
        if current:
            pis.append(current)
    fields["主要研究者"] = pis
    status_section = re.split(r"六、\s*试验状态信息", body, maxsplit=1)
    if len(status_section) != 2:
        raise ValueError("缺少详情试验状态，不能确认是否进行中")
    status_match = re.search(r"进行中\s*[（(]\s*([^）)]+)[）)]|已完成|主动暂停|主动终止|IEC/IRB暂停|IEC/IRB终止|责令暂停|责令终止", status_section[1])
    if not status_match:
        raise ValueError("无法识别详情试验状态")
    fields["试验状态"] = clean(status_match.group(0))
    fields["抓取时间"] = now()
    fields["原始页面SHA256"] = hashlib.sha256(html.encode()).hexdigest()
    return fields


def highlights(items, max_items=8):
    """Verbatim selection, never invent or rewrite medical thresholds."""
    important = re.compile(r"年龄|ECOG|体能|病理|组织学|突变|阳性|阴性|既往|治疗|脑|中枢|转移|器官|骨髓|感染|妊娠|怀孕|肺病|心")
    selected = [(i, text) for i, text in enumerate(items) if important.search(text)]
    if not selected:
        selected = list(enumerate(items))
    result = "\n".join(f"{i + 1}. {text}" for i, text in selected[:max_items])
    return result if len(result) <= 30000 else result[:29900] + "\n[过长内容请查看入排全文]"


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "raw").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.directory / "progress.sqlite3")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trials(ctr TEXT PRIMARY KEY, summary TEXT NOT NULL,
          detail TEXT, status TEXT DEFAULT 'pending', error TEXT DEFAULT '', attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS matches(keyword TEXT, ctr TEXT, PRIMARY KEY(keyword,ctr));
        CREATE TABLE IF NOT EXISTS searches(keyword TEXT PRIMARY KEY, declared INTEGER, pages INTEGER,
          complete INTEGER DEFAULT 0, error TEXT DEFAULT '', checked_at TEXT);
        """)

    def add(self, keyword, row):
        self.db.execute("INSERT OR IGNORE INTO trials(ctr,summary) VALUES (?,?)", (row[1], json.dumps(row, ensure_ascii=False)))
        self.db.execute("INSERT OR IGNORE INTO matches VALUES (?,?)", (keyword, row[1]))
        self.db.commit()

    def done(self, ctr):
        row = self.db.execute("SELECT status FROM trials WHERE ctr=?", (ctr,)).fetchone()
        return row and row[0] == "done"

    def save(self, ctr, detail, html):
        if html is not None:
            (self.directory / "raw" / f"{ctr}.html").write_text(html, encoding="utf-8")
        self.db.execute("UPDATE trials SET detail=?,status='done',error='' WHERE ctr=?", (json.dumps(detail, ensure_ascii=False), ctr))
        self.db.commit()

    def fail(self, ctr, error):
        self.db.execute("UPDATE trials SET status='failed',error=?,attempts=attempts+1 WHERE ctr=?", (str(error)[:1500], ctr))
        self.db.commit()

    def search_status(self, keyword, declared, pages, complete, error=""):
        self.db.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?,?)", (keyword, declared, pages, int(complete), error, now()))
        self.db.commit()


def export(store):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.table import Table, TableStyleInfo
    recruiting_only = getattr(store, "recruiting_only", False)
    wb = Workbook()
    info = wb.active
    info.title = "采集说明"
    info.append(["项目", "内容"])
    info.append(["导出时间", now()])
    info.append(["范围", "进行中的肿瘤相关候选试验，包含尚未招募、招募中、招募完成；健康受试者试验保留标记。关键词检索不能自动证明全库完整性。"])
    info.append(["入排标准", "重点摘录为规则选取的原文，可能遗漏队列和例外条款。完整标准在入排全文工作表，独立浏览器采集的原网页保存于raw目录；扩展导入保留完整标准，但不含原网页快照。"])
    info.append(["联系人", "主要研究者与申请人联系人分列；所有信息来自公示页面。"])
    if recruiting_only:
        info.cell(3, 2).value = "仅纳入详情状态为进行中且招募中的肿瘤相关候选试验；排除尚未招募、招募完成。关键词覆盖仍须复核。"
    searches = list(store.db.execute("SELECT * FROM searches ORDER BY keyword"))
    remaining = store.db.execute("SELECT count(*) FROM trials WHERE status!='done'").fetchone()[0]
    complete = bool(searches) and all(r[3] for r in searches) and remaining == 0
    info.append(["完成情况", "已完成本次关键词集合的页面采集，仍须复核肿瘤范围" if complete else "未完成：请查看检索覆盖及待补采工作表，重新启动可继续采集"])
    ws = wb.create_sheet("招募中项目" if recruiting_only else "进行中项目")
    headers = ["CTR", "项目名", "关键入选原文摘录", "关键排除原文摘录", "主要研究者", "研究者单位", "研究者电话", "研究者邮箱", "申请人联系人", "申请人电话", "申请人邮箱", "试验状态", "适应症", "药物名称", "试验分期", "健康受试者", "匹配关键词", "抓取时间", "官网检索链接"]
    order = [0,1,2,3,8,9,10,4,5,6,7,*range(11,len(headers))] if recruiting_only else list(range(len(headers)))
    headers = [headers[i] for i in order]
    ws.append(headers)
    full = wb.create_sheet("入排全文")
    full.append(["CTR", "类型", "原文序号", "标准原文"])
    failed = wb.create_sheet("待补采")
    failed.append(["CTR", "列表项目名", "采集状态", "错误信息"])
    changed = wb.create_sheet("非招募中记录" if recruiting_only else "状态已变更")
    changed.append(["CTR", "项目名", "详情状态", "抓取时间"])
    rows_out = []
    for ctr, summary, detail, state, error, attempts in store.db.execute("SELECT * FROM trials ORDER BY ctr DESC"):
        summary = json.loads(summary)
        if state != "done":
            failed.append([ctr, summary[5], state, error])
            continue
        # XML 1.0 forbids control characters occasionally embedded in public text.
        def clean_xml(value):
            if isinstance(value, str): return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
            if isinstance(value, list): return [clean_xml(v) for v in value]
            if isinstance(value, dict): return {k: clean_xml(v) for k, v in value.items()}
            return value
        d = clean_xml(json.loads(detail))
        if not d["试验状态"].startswith("进行中") or (recruiting_only and "招募中" not in d["试验状态"]):
            changed.append([ctr, d.get("试验专业题目"), d["试验状态"], d["抓取时间"]])
            continue
        pis = d["主要研究者"]
        def people(key):
            return "\n".join(p.get(key, "页面未公开") for p in pis) or "页面未公开"
        keywords = "、".join(r[0] for r in store.db.execute("SELECT keyword FROM matches WHERE ctr=? ORDER BY keyword", (ctr,)))
        row = [ctr, d.get("试验专业题目") or d.get("试验通俗题目"), highlights(d["入选标准"]), highlights(d["排除标准"]), people("姓名"), people("单位名称"), people("电话"), people("Email"), d.get("联系人姓名", "页面未公开"), " / ".join(filter(None, [d.get("联系人座机"), d.get("联系人手机号")])) or "页面未公开", d.get("联系人Email", "页面未公开"), d["试验状态"], d.get("适应症"), d.get("药物名称"), d.get("试验分期"), d.get("健康受试者"), keywords, d["抓取时间"], BASE + "/clinicaltrials.searchlist.dhtml?keywords=" + quote(ctr)]
        row = [row[i] for i in order]
        rows_out.append(row)
        ws.append(row)
        for kind in ("入选标准", "排除标准"):
            for i, text in enumerate(d[kind], 1):
                # Excel has a 32767-character cell limit: preserve long text in chunks.
                for offset in range(0, len(text), 30000):
                    full.append([ctr, kind, i, text[offset:offset + 30000]])
    coverage = wb.create_sheet("检索覆盖")
    coverage.append(["关键词", "官网声明条数", "读取页数", "翻页完成", "已发现CTR数", "错误信息", "检查时间"])
    for key, declared, pages, complete_search, error, stamp in searches:
        count = store.db.execute("SELECT count(*) FROM matches WHERE keyword=?", (key,)).fetchone()[0]
        coverage.append([key, declared, pages, "是" if complete_search else "否", count, error, stamp])
    for index, sheet in enumerate(wb):
        sheet.freeze_panes = "B2" if sheet == ws else "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24445C")
        for cells in sheet.iter_rows(min_row=2):
            for cell in cells:
                if isinstance(cell.value, str):
                    cell.data_type = "s"  # public text must not become Excel formulas
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = 24
        if sheet.max_row > 1:
            t = Table(displayName=f"Data{index}", ref=sheet.dimensions)
            t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            sheet.add_table(t)
    info.column_dimensions["B"].width = 110
    for col in ("B", "C", "D"):
        ws.column_dimensions[col].width = 65
    full.column_dimensions["D"].width = 120
    failed.column_dimensions["B"].width = 65
    failed.column_dimensions["D"].width = 80
    for sheet in (ws, full, info):
        for row in range(2, sheet.max_row + 1):
            sheet.row_dimensions[row].height = 90 if sheet == ws else 60
    temp = store.directory / "肿瘤项目.tmp.xlsx"
    wb.save(temp)
    temp.replace(store.directory / "肿瘤项目.xlsx")
    with (store.directory / "肿瘤项目.csv").open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(headers)
        # Prevent formula interpretation when opening CSV in Excel.
        writer.writerows([["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for v in row] for row in rows_out])
    print(f"已导出 {len(rows_out)} 条详情，待补采 {remaining} 条 → {store.directory / '肿瘤项目.xlsx'}", flush=True)


def human_recovery(page, message, interactive):
    if not interactive:
        raise RuntimeError(message)
    print(f"\n{message}\n如页面出现验证，请在浏览器手动完成。不要关闭窗口。", flush=True)
    if input("处理完成后按回车重试，输入 q 停止：").strip().lower() == "q":
        raise KeyboardInterrupt


def open_search(page, timeout, interactive):
    statuses = []
    def record_response(response):
        if response.request.resource_type == "document":
            statuses.append(response.status)
    page.on("response", record_response)
    form = page.get_by_role("textbox", name="适应症", exact=True)
    try:
        page.goto(BASE + "/clinicaltrials.prosearch.dhtml?pro=y", wait_until="domcontentloaded", timeout=timeout)
        form.wait_for(timeout=timeout)
    except Exception as exc:
        message = f"官网检索表单未加载（HTTP状态序列：{statuses or '未收到响应'}）。"
        if any(status >= 400 for status in statuses):
            message += "服务器拒绝了当前访问。"
        elif 202 in statuses:
            message += "网站返回验证响应，未进入检索页。"
        try:
            human_recovery(page, message, interactive)
            form.wait_for(timeout=timeout)
        except Exception as recovery_error:
            raise WebsiteAccessError(message) from recovery_error
    finally:
        page.remove_listener("response", record_response)
    return form


def listing(page, timeout):
    page.wait_for_function("""() => /共\\s*\\d+\\s*条记录|没有.*记录|无.*结果/.test(document.body.innerText)""", timeout=timeout)
    text = page.locator("body").inner_text()
    match = re.search(r"当前第\s*(\d+)\s*页，共\s*(\d+)\s*页，共\s*(\d+)\s*条记录", text)
    if not match:
        if re.search(r"共\s*0\s*条记录|没有.*记录|无.*结果", text):
            return [], 1, 1, 0
        raise ValueError("无法识别列表分页，停止以免漏页")
    rows = page.locator("table tr").evaluate_all("""rows => rows.map(r => Array.from(r.children).filter(c => ['TD','TH'].includes(c.tagName)).map(c => c.innerText.trim())).filter(r => r.length === 6 && /^CTR\\d{8}$/.test(r[1]))""")
    current, pages, total = map(int, match.groups())
    if total and not rows:
        raise ValueError("列表声明非空但读取到0条")
    return rows, current, pages, total


def collect(args):
    from playwright.sync_api import sync_playwright
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    directory = Path(args.output or ROOT / config["output_dir"]).resolve()
    store = Store(directory)
    store.recruiting_only = bool(config.get("recruiting_only", False))
    if getattr(args, 'import_browser', None):
        try:
            data=json.loads(Path(args.import_browser).read_text(encoding='utf-8'))
            if data.get('format')!='cde-browser-v1':
                raise ValueError('不是CDE浏览器采集文件')
            for record in data['records']:
                ctr=record['ctr']; d=record['detail']
                if not CTR.fullmatch(ctr) or d.get('登记号')!=ctr or not d.get('入选标准') or not d.get('排除标准') or len(record.get('summary', [])) != 6 or record['summary'][1] != ctr:
                    raise ValueError('浏览器记录字段不完整或CTR不匹配')
                for keyword in record.get('keywords') or ['浏览器采集']:
                    store.add(keyword,record['summary'])
                store.save(ctr,d,None)
            for failure in data.get('failures', {}).values():
                ctr = failure.get('ctr', '')
                summary = failure.get('summary', [])
                if not CTR.fullmatch(ctr) or len(summary) != 6 or summary[1] != ctr:
                    raise ValueError('待补采记录标识不完整')
                if store.done(ctr):
                    continue
                store.add(failure.get('keyword') or '浏览器采集', summary)
                store.fail(ctr, failure.get('error') or '详情待补采')
                if failure.get('html'):
                    (store.directory / 'raw' / f'{ctr}.html').write_text(failure['html'], encoding='utf-8')
            for c in data.get('coverage',[]):
                store.search_status(c['keyword'],c['declared'],c['pages'],c['complete'],c.get('error',''))
            export(store)
            return 0
        finally:
            store.db.close()
    if args.export_only:
        try:
            export(store)
            return 0
        finally:
            store.db.close()
    timeout = int(config.get("timeout_seconds", 45) * 1000)
    delay = max(2, float(config.get("delay_seconds", 4)))
    keywords = args.keywords or config["keywords"]
    if args.refresh:
        store.db.execute("UPDATE trials SET status='pending',error='' ")
        store.db.execute("DELETE FROM matches")
        store.db.execute("DELETE FROM searches")
        store.db.commit()
    processed = 0
    consecutive_failures = 0
    exit_code = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, channel=args.channel or None)
            context = browser.new_context()
            context.set_default_timeout(timeout)
            page = context.new_page()
            try:
                for keyword in keywords:
                    current = total = 0
                    try:
                        form = open_search(page, timeout, not args.non_interactive)
                        form.fill(keyword)
                        page.get_by_role("combobox").nth(1).select_option(label="进行中")
                        page.get_by_role("button", name="查询", exact=True).click()
                        seen_pages = set()
                        initial_total = None
                        while True:
                            rows, current, pages, total = listing(page, timeout)
                            if initial_total is None:
                                initial_total = total
                            signature = tuple(row[1] for row in rows)
                            if signature in seen_pages:
                                raise ValueError("翻页返回重复列表")
                            seen_pages.add(signature)
                            store.search_status(keyword, total, current, False)
                            for row in rows:
                                store.add(keyword, row)
                            print(f"[{keyword}] 第 {current}/{pages} 页，官网 {total} 条", flush=True)
                            for row in rows:
                                ctr = row[1]
                                if store.done(ctr):
                                    continue
                                success = False
                                for attempt in range(config.get("max_retries", 2)):
                                    popup = None
                                    try:
                                        time.sleep(delay)
                                        # CDE opens details asynchronously in a new tab.
                                        with page.expect_popup(timeout=timeout) as popup_info:
                                            page.get_by_role("link", name=ctr, exact=True).click()
                                        popup = popup_info.value
                                        popup.wait_for_load_state("domcontentloaded")
                                        popup.get_by_role("button", name=re.compile("公示的试验信息")).click()
                                        popup.get_by_text("排除标准", exact=True).wait_for(state="visible")
                                        html = popup.content()
                                        detail = parse_detail(html, ctr)
                                        store.save(ctr, detail, html)
                                        processed += 1
                                        success = True
                                        consecutive_failures = 0
                                        print(f"  已保存 {ctr} / {detail['试验状态']}", flush=True)
                                        break
                                    except Exception as exc:
                                        store.fail(ctr, exc)
                                        print(f"  {ctr} 第{attempt + 1}次失败：{str(exc)[:180]}", flush=True)
                                        if attempt == 0 and not args.non_interactive:
                                            human_recovery(popup or page, "详情未读全，可检查网站是否要求人工验证。", True)
                                    finally:
                                        if popup and not popup.is_closed():
                                            popup.close()
                                if not success:
                                    print(f"  {ctr} 已列入待补采", flush=True)
                                    consecutive_failures += 1
                                    if consecutive_failures >= 3:
                                        print("连续3条详情失败，停止本次任务，保留进度以便检查网站访问。", flush=True)
                                        return 2
                                if processed and processed % 20 == 0:
                                    export(store)
                                if args.limit and processed >= args.limit:
                                    print("达到本次采集上限，清单明确标注未完成。", flush=True)
                                    return 0
                            if current >= pages:
                                found = store.db.execute("SELECT count(*) FROM matches WHERE keyword=?", (keyword,)).fetchone()[0]
                                okay = found == total == initial_total
                                store.search_status(keyword, total, current, okay, "" if okay else f"数量不一致：初始{initial_total}，末页{total}，发现{found}；请重新核查")
                                if not okay:
                                    exit_code = 2
                                break
                            time.sleep(delay)
                            page.get_by_role("link", name="Next", exact=True).click()
                            # Wait for a changed page number, not only DOMContentLoaded.
                            page.wait_for_function("""previous => { const m = document.body.innerText.match(/当前第\\s*(\\d+)\\s*页/); return m && Number(m[1]) > previous; }""", arg=current, timeout=timeout)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        exit_code = 2
                        store.search_status(keyword, total, current, False, str(exc)[:1500])
                        print(f"[{keyword}] 未完成：{str(exc)[:180]}", flush=True)
                        try:
                            diagnostic = directory / ("访问失败-" + hashlib.sha256(keyword.encode()).hexdigest()[:8])
                            diagnostic.with_suffix(".html").write_text(page.content(), encoding="utf-8")
                            page.screenshot(path=str(diagnostic.with_suffix(".png")))
                            print(f"页面诊断已保存在 {diagnostic.name}.*", flush=True)
                        except Exception:
                            pass
                        if isinstance(exc, WebsiteAccessError):
                            print("官网入口不可访问，停止本次采集，避免重复请求所有关键词。", flush=True)
                            break
            finally:
                context.close()
                browser.close()
    except KeyboardInterrupt:
        print("已停止，进度已保留。", flush=True)
        exit_code = 130
    finally:
        export(store)
        store.db.close()
    return exit_code


def main():
    parser = argparse.ArgumentParser(description="CDE进行中肿瘤试验采集器")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--output", help="保存目录")
    parser.add_argument("--keywords", nargs="+", help="覆盖默认关键词")
    parser.add_argument("--limit", type=int, default=0, help="本次新增详情上限，0为不限")
    parser.add_argument("--refresh", action="store_true", help="重新核查已有记录状态")
    parser.add_argument("--export-only", action="store_true", help="只导出已保存数据")
    parser.add_argument("--import-browser", help="将浏览器扩展导出的JSON转换为Excel")
    parser.add_argument("--non-interactive", action="store_true", help="失败时记录并继续，不请求人工处理")
    parser.add_argument("--channel", choices=["chrome", "msedge"], help="使用已安装的浏览器")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    return collect(args)


if __name__ == "__main__":
    sys.exit(main())
