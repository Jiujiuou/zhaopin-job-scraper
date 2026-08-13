# -*- coding: utf-8 -*-
"""
智联招聘通用抓取+筛选脚本 (zhaopin-job-scraper)
================================================
一个脚本完成：抓取 → 过滤 → 生成 HTML 报告

用法:
    python zhaopin_jobs.py --config config.json --session <bsk_session_id>

配置项 (JSON):
    {
      "job_title": "前端开发",              # 报告标题用
      "keywords": ["前端", "web前端"],       # 智联搜索关键词（必填，可多个）
      "city": "深圳",                       # 城市名（必填，见 cities.json）或 jl 编码如 "765"
      "salary_min_k": 20,                   # 薪资下限 K（必填，0 表示不限）
      "salary_max_k": 100,                  # 薪资上限 K（可选）
      "blacklist": ["中软国际", "软通"],      # 公司黑名单（可选）
      "include_terms": ["前端", "vue"],      # 标题包含词（可选，空=全部通过）
      "exclude_terms": ["后端", "测试"],      # 标题排除词（可选）
      "dispatch_terms": ["派遣"],            # 派遣/外包排除词（可选）
      "hr_active_days": 7,                  # HR 活跃阈值天（可选，默认7）
      "max_pages": 12,                      # 每关键词最大页数（可选，默认12）
      "output_dir": ".",                    # 报告输出目录（可选）
      "sort_by": "salary"                   # 排序: salary=薪资优先, hr=活跃优先（可选）
    }

依赖:
    - bsk CLI (BrowserSkill) 已安装并连接浏览器
    - Python 3.8+
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

BSK = os.environ.get("BSK_PATH", "bsk")
HERE = os.path.dirname(os.path.abspath(__file__))
CITY_MAP_PATH = os.path.join(HERE, "..", "cities.json")

# ============ 智联 DOM 选择器（2026-08 验证） ============
CARD_SELECTOR = ".joblist-box__item.clearfix"
SEL_TITLE = ".jobinfo__name"
SEL_SALARY = ".jobinfo__salary"
SEL_COMPANY = ".companyinfo__name"
SEL_HR = ".companyinfo__staff-name"
SEL_HR_REPLY = ".companyinfo__staff"
SEL_AREA = ".jobinfo__other-info-item"

EXTRACT_JS = """(() => {
  const cards = document.querySelectorAll('%s');
  const jobs = Array.from(cards).map(c => {
    const a = c.querySelector('a[href*="jobdetail"]');
    const t = c.querySelector('%s');
    const s = c.querySelector('%s');
    const co = c.querySelector('%s');
    const hr = c.querySelector('%s');
    const hrReply = (c.querySelector('%s') || {}).innerText || '';
    const reply = (hrReply.match(/(刚刚回复|\\d+分钟前回复|\\d+小时内回复|\\d+天前回复|回复可能性大)/) || [])[0] || '';
    const area = (c.querySelector('%s') || {}).innerText || '';
    return {
      title: t ? t.innerText.trim() : '',
      salary: s ? s.innerText.trim() : '',
      company: co ? co.innerText.trim() : '',
      hr: hr ? hr.innerText.trim() : '',
      hr_reply: reply,
      area: area.trim(),
      href: a ? a.href.split('?')[0] : ''
    };
  });
  return JSON.stringify({count: jobs.length, jobs});
})()""" % (CARD_SELECTOR, SEL_TITLE, SEL_SALARY, SEL_COMPANY, SEL_HR, SEL_HR_REPLY, SEL_AREA)


# ============ 薪资格式解析（支持 万/K/元/面议） ============
def parse_salary_max(salary_str):
    """返回薪资上限（K）。无法解析返回 0"""
    if not salary_str or "面议" in salary_str:
        return 0
    s = salary_str.strip()
    # 万: 1.8-2.4万
    m = re.search(r'(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*万', s)
    if m:
        return int(float(m.group(2)) * 10)
    m = re.search(r'(\d+(?:\.\d+)?)\s*万', s)
    if m:
        return int(float(m.group(1)) * 10)
    # 元: 8000-15000元
    m = re.search(r'(\d+)\s*[-~至]\s*(\d+)\s*元', s)
    if m:
        return int(int(m.group(2)) / 1000)
    m = re.search(r'(\d+)\s*元', s)
    if m:
        return int(int(m.group(1)) / 1000)
    # K: 15-22K / 20K
    m = re.search(r'(\d+)\s*[-~至]\s*(\d+)\s*[Kk]', s)
    if m:
        return int(m.group(2))
    m = re.search(r'(\d+)\s*[Kk]', s)
    if m:
        return int(m.group(1))
    return 0


def salary_min_k(salary_str):
    """返回薪资下限（K）。用于过滤低于期望的岗位"""
    if not salary_str or "面议" in salary_str:
        return 0
    s = salary_str.strip()
    m = re.search(r'(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*万', s)
    if m:
        return int(float(m.group(1)) * 10)
    m = re.search(r'(\d+)\s*[-~至]\s*(\d+)\s*元', s)
    if m:
        return int(int(m.group(1)) / 1000)
    m = re.search(r'(\d+)\s*[-~至]\s*(\d+)\s*[Kk]', s)
    if m:
        return int(m.group(1))
    return parse_salary_max(s)


# ============ HR 活跃度 ============
def hr_active_score(hr_reply, active_days=7):
    """返回 2=非常活跃(<=1天), 1=活跃(<=active_days), 0=未知"""
    if not hr_reply:
        return 0
    if "刚刚回复" in hr_reply:
        return 2
    m = re.search(r'(\d+)分钟前回复', hr_reply)
    if m:
        return 2 if int(m.group(1)) <= 60 else 1
    m = re.search(r'(\d+)小时内回复', hr_reply)
    if m:
        return 2 if int(m.group(1)) <= 24 else 1
    m = re.search(r'(\d+)天前回复', hr_reply)
    if m:
        return 1 if int(m.group(1)) <= active_days else 0
    if "回复可能性大" in hr_reply:
        return 1
    return 0


# ============ bsk 交互 ============
def run_bsk(args, timeout=30):
    cmd = [BSK] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.stdout
    except subprocess.TimeoutExpired:
        print(f"[WARN] bsk timeout: {args[:3]}")
        return ""


def extract_page(session_id):
    out = run_bsk(["evaluate", "--session", session_id, "--json", EXTRACT_JS])
    try:
        d = json.loads(out)
        if d.get("ok"):
            v = json.loads(d["value"])
            return v.get("jobs", [])
    except Exception as e:
        print(f"[ERR] parse failed: {e}")
    return []


def resolve_keyword_code(session_id, keyword, city_code):
    """导航搜索页，读取智联加密的关键词编码"""
    from urllib.parse import quote
    url = f"https://sou.zhaopin.com/?kw={quote(keyword)}&jl={city_code}"
    run_bsk(["navigate", url, "--session", session_id, "--timeout", "30000"])
    time.sleep(3)
    out = run_bsk(["evaluate", "--session", session_id, "--json",
                   "(() => { return JSON.stringify({url: location.href}); })()"])
    try:
        d = json.loads(out)
        v = json.loads(d["value"])
        m = re.search(r"/kw([A-Za-z0-9]+)/p1", v["url"])
        if m:
            return m.group(1)
        m2 = re.search(r"/kw([A-Za-z0-9]+)", v["url"])
        if m2:
            return m2.group(1)
    except Exception:
        pass
    return ""


def crawl_keyword(session_id, keyword, city_code, config, all_jobs, seen):
    """抓取单个关键词的所有分页"""
    kw_code = resolve_keyword_code(session_id, keyword, city_code)
    if not kw_code:
        print(f"[ERR] 无法获取关键词 '{keyword}' 的编码，跳过")
        return
    print(f"[INFO] 关键词 '{keyword}' → 编码 {kw_code}")

    max_pages = config.get("max_pages", 12)
    for page in range(1, max_pages + 1):
        url = f"https://www.zhaopin.com/sou/jl{city_code}/kw{kw_code}/p{page}"
        sl = config.get("salary_min_k", 0)
        if sl:
            url += f"?sl={sl * 1000},1000000"
        print(f"[INFO] 翻页 {page}: {url[:80]}")
        run_bsk(["navigate", url, "--session", session_id, "--timeout", "30000"])
        time.sleep(3)
        jobs = extract_page(session_id)
        added = 0
        for j in jobs:
            if not j.get("href"):
                continue
            key = j["href"].split("/")[-1]
            j["city"] = config.get("city", "")
            j["keyword"] = keyword
            if key not in seen:
                seen.add(key)
                all_jobs[key] = j
                added += 1
        print(f"[INFO] 本页 {len(jobs)} 条, 新增 {added}, 累计 {len(all_jobs)}")
        if added == 0 and len(jobs) == 0:
            break
        time.sleep(1)


# ============ 过滤 ============
def filter_jobs(jobs, config):
    passed, filtered = [], []
    include = [x.lower() for x in config.get("include_terms", [])]
    exclude = [x.lower() for x in config.get("exclude_terms", [])]
    blacklist = config.get("blacklist", [])
    dispatch = config.get("dispatch_terms", ["派遣"])
    salary_min = config.get("salary_min_k", 0)
    active_days = config.get("hr_active_days", 7)

    for job in jobs:
        title = job.get("title", "")
        company = job.get("company", "")
        salary = job.get("salary", "")
        hr_reply = job.get("hr_reply", "")
        reasons = []

        # 黑名单公司
        if any(b.lower() in company.lower() for b in blacklist if b):
            reasons.append(f"黑名单: {company[:15]}")
        # 派遣
        if any(d in title for d in dispatch):
            reasons.append("派遣岗")
        # 包含词（可选）：提供则必须命中至少一个
        if include and not any(x in title.lower() for x in include):
            reasons.append("标题不含指定词")
        # 排除词（可选）：命中即排除
        if exclude and any(x in title.lower() for x in exclude):
            reasons.append("标题含排除词")
        # 薪资
        max_k = parse_salary_max(salary)
        if not max_k:
            reasons.append(f"薪资未知: {salary}")
        elif max_k < salary_min:
            reasons.append(f"薪资过低: {salary} (上限{max_k}K<{salary_min}K)")

        score = hr_active_score(hr_reply, active_days)
        if reasons:
            filtered.append({**job, "reasons": reasons})
        else:
            passed.append({**job, "hr_score": score})
    return passed, filtered


# ============ HTML 报告 ============
def card_html(j):
    hr_map = {2: ("hr-active", "非常活跃"), 1: ("hr-mid", "活跃"), 0: ("hr-unknown", "未知")}
    cls, label = hr_map.get(j.get("hr_score", 0), ("hr-unknown", "未知"))
    kw = j.get("keyword", "")
    kw_tag = f'<span class="tag">{kw}</span>' if kw else ""
    return f'''
  <div class="job-card priority-{cls}">
    <div class="card-header">
      <div class="job-title">{j.get("title", "")}</div>
      <span class="salary-badge">{j.get("salary", "未知")}</span>
    </div>
    <div class="company-row">
      <span class="company-name">{j.get("company", "(需确认)")}</span>
      <span class="area-name">{j.get("area", "")}</span>
    </div>
    <div class="tags-row">{kw_tag}<span class="tag">HR{label}</span></div>
    <div class="card-footer">
      <span class="hr-badge {cls}">{j.get("hr", "未知")} · {j.get("hr_reply", "回复时间未知")}</span>
      <a href="{j.get("href", "#")}" target="_blank" class="view-btn">查看详情 →</a>
    </div>
  </div>'''


def generate_html(passed, filtered, total, config, output_path):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sort_by = config.get("sort_by", "salary")
    if sort_by == "hr":
        passed.sort(key=lambda j: (-j.get("hr_score", 0), -parse_salary_max(j.get("salary", ""))))
    else:
        passed.sort(key=lambda j: (-parse_salary_max(j.get("salary", "")), -j.get("hr_score", 0)))

    cards = "".join(card_html(j) for j in passed)

    filtered_html = ""
    for fj in filtered[:30]:
        reasons_str = " | ".join(fj["reasons"])
        filtered_html += f'''
    <li class="filtered-item">
      <span>{fj.get("title", "?")} · {fj.get("company") or "?"} · {fj.get("salary") or "?"}</span>
      <span class="filter-reason">{reasons_str}</span>
    </li>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{config.get("job_title", "岗位")} | {config.get("city", "")} | 薪资≥{config.get("salary_min_k", 0)}K</title>
<style>
  :root {{
    --bg:#fafafa; --surface:#fff; --text:#1a1a2e; --text-secondary:#6b7280;
    --accent:#2563eb; --accent-soft:#eff6ff; --success:#059669; --success-soft:#ecfdf5;
    --warning:#d97706; --warning-soft:#fffbeb; --border:#e5e7eb;
    --shadow:0 1px 3px rgba(0,0,0,.08); --radius:8px;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:var(--font); background:var(--bg); color:var(--text); line-height:1.6; padding:24px; max-width:1200px; margin:0 auto; }}
  .header {{ margin-bottom:32px; padding-bottom:20px; border-bottom:1px solid var(--border); }}
  .header h1 {{ font-size:24px; font-weight:700; letter-spacing:-.5px; margin-bottom:8px; }}
  .header-meta {{ display:flex; gap:16px; flex-wrap:wrap; color:var(--text-secondary); font-size:13px; }}
  .header-meta span {{ background:var(--surface); padding:4px 10px; border-radius:4px; border:1px solid var(--border); }}
  .stats-bar {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:28px; }}
  .stat-card {{ background:var(--surface); padding:16px; border-radius:var(--radius); box-shadow:var(--shadow); text-align:center; }}
  .stat-number {{ font-size:28px; font-weight:800; }}
  .stat-label {{ font-size:12px; color:var(--text-secondary); margin-top:2px; }}
  .section-title {{ font-size:15px; font-weight:600; margin-bottom:14px; display:flex; align-items:center; gap:8px; }}
  .section-title::before {{ content:''; width:3px; height:16px; background:var(--accent); border-radius:2px; }}
  .job-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:14px; margin-bottom:32px; }}
  .job-card {{ background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow); padding:18px; transition:all .2s; border-left:3px solid transparent; }}
  .job-card:hover {{ transform:translateY(-1px); }}
  .priority-hr-active {{ border-left-color:var(--success); }}
  .priority-hr-mid {{ border-left-color:var(--warning); }}
  .priority-hr-unknown {{ border-left-color:var(--border); }}
  .card-header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }}
  .job-title {{ font-size:15px; font-weight:600; flex:1; margin-right:8px; }}
  .salary-badge {{ font-size:14px; font-weight:700; color:var(--success); background:var(--success-soft); padding:3px 10px; border-radius:6px; white-space:nowrap; }}
  .company-row {{ display:flex; align-items:center; gap:8px; margin-bottom:10px; font-size:13px; color:var(--text-secondary); }}
  .company-name {{ font-weight:500; color:var(--text); }}
  .tags-row {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; }}
  .tag {{ font-size:11px; padding:2px 8px; border-radius:4px; background:#f3f4f6; color:var(--text-secondary); }}
  .card-footer {{ display:flex; justify-content:space-between; align-items:center; padding-top:12px; border-top:1px solid var(--border); }}
  .hr-badge {{ font-size:11px; padding:3px 8px; border-radius:4px; white-space:nowrap; }}
  .hr-active {{ background:var(--success-soft); color:var(--success); }}
  .hr-mid {{ background:var(--warning-soft); color:var(--warning); }}
  .hr-unknown {{ background:#f3f4f6; color:var(--text-secondary); }}
  .view-btn {{ padding:6px 14px; font-size:13px; font-weight:500; color:#fff; background:var(--text); border:none; border-radius:6px; cursor:pointer; text-decoration:none; }}
  .view-btn:hover {{ background:var(--accent); }}
  .filtered-section {{ background:#fff8f8; border:1px solid #fecaca; border-radius:var(--radius); padding:18px; margin-bottom:32px; }}
  .filtered-list {{ list-style:none; }}
  .filtered-item {{ padding:8px 0; border-bottom:1px solid #fecaca; font-size:13px; display:flex; justify-content:space-between; align-items:center; }}
  .filter-reason {{ font-size:11px; padding:2px 8px; background:#dc2626; color:#fff; border-radius:4px; white-space:nowrap; }}
  .footer-note {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:16px 20px; font-size:13px; color:var(--text-secondary); line-height:1.7; }}
  @media (max-width:640px) {{ body {{ padding:12px; }} .job-grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>🎯 {config.get("job_title", "岗位")}筛选报告</h1>
  <div class="header-meta">
    <span>📅 {now}</span>
    <span>📍 {config.get("city", "")}</span>
    <span>💰 薪资 ≥ {config.get("salary_min_k", 0)}K</span>
    <span>🔍 关键词: {' / '.join(config.get("keywords", []))}</span>
    <span>🔄 智联直抓 + 薪资硬过滤</span>
  </div>
</div>
<div class="stats-bar">
  <div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">抓取总数</div></div>
  <div class="stat-card"><div class="stat-number">{len(passed)}</div><div class="stat-label">✅ 符合条件</div></div>
  <div class="stat-card"><div class="stat-number">{len(filtered)}</div><div class="stat-label">❌ 已过滤</div></div>
</div>
<h2 class="section-title">✅ 符合条件（{len(passed)} 个）</h2>
<div class="job-grid">{cards}</div>
<div class="filtered-section">
  <h3 class="section-title" style="margin-bottom:12px;">❌ 已过滤岗位（前30条）</h3>
  <ul class="filtered-list">{filtered_html}</ul>
</div>
<div class="footer-note">
  <strong>📌 说明：</strong><br>
  • 数据来源：智联招聘搜索，列表页直接抓取（无需点详情页）<br>
  • 过滤规则：薪资上限 / 黑名单公司 / 包含排除词 / 派遣岗<br>
  • 卡片绿色=HR非常活跃(≤1天)，黄色=活跃(≤7天)，灰色=未知<br>
  • <em>— zhaopin-job-scraper 自动生成 · {now}</em>
</div>
</body>
</html>'''

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] 报告: {output_path}")
    print(f"[OK] 通过 {len(passed)}, 过滤 {len(filtered)}")
    return output_path


# ============ 主流程 ============
def load_city_map():
    try:
        with open(CITY_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_city_code(session_id, city_name):
    """解析城市 jl 编码：先查本地表，查不到则动态从智联 citymap 页面获取"""
    city_map = load_city_map()
    if city_name in city_map:
        return city_map[city_name]

    # 动态获取：打开 citymap，找中文城市名对应的拼音链接
    print(f"[INFO] 城市 '{city_name}' 不在本地表，正在从智联动态获取编码...")
    run_bsk(["navigate", "https://www.zhaopin.com/citymap", "--session", session_id, "--timeout", "30000"])
    time.sleep(3)
    js = """(() => {
      const links = Array.from(document.querySelectorAll('a'));
      const target = links.find(a => a.innerText && a.innerText.trim() === '%s' && a.href.indexOf('zhaopin.com/') > 0);
      return target ? target.href : '';
    })()""" % city_name
    out = run_bsk(["evaluate", "--session", session_id, "--json", js])
    try:
        d = json.loads(out)
        raw_val = d.get("value", "") if d.get("ok") else ""
        try:
            city_url = json.loads(raw_val) if raw_val.startswith('"') else raw_val
        except Exception:
            city_url = raw_val
    except Exception:
        city_url = ""
    if not city_url:
        print(f"[ERR] citymap 页面找不到城市 '{city_name}'，请检查城市名拼写，或直接填 jl 编码")
        return ""

    # 访问城市拼音页，从 HTML 提取 jl 编码
    run_bsk(["navigate", city_url, "--session", session_id, "--timeout", "60000"])
    time.sleep(5)
    out = run_bsk(["evaluate", "--session", session_id, "--json",
                   "(() => { const m = document.body.innerHTML.match(/jl(\\d+)/); return m ? m[1] : ''; })()"])
    try:
        d = json.loads(out)
        raw_val = d.get("value", "") if d.get("ok") else ""
        try:
            code = json.loads(raw_val) if raw_val.startswith('"') else raw_val
        except Exception:
            code = raw_val
    except Exception:
        code = ""
    if code:
        city_map[city_name] = code
        with open(CITY_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(city_map, f, ensure_ascii=False, indent=1)
        print(f"[OK] 城市 '{city_name}' → 编码 {code}（已加入本地表）")
        return code
    print(f"[ERR] 无法获取城市 '{city_name}' 的编码，请直接填 jl 编码")
    return ""


def main():
    parser = argparse.ArgumentParser(description="智联招聘通用抓取筛选")
    parser.add_argument("--config", required=True, help="配置文件路径")
    parser.add_argument("--session", required=True, help="bsk session id")
    parser.add_argument("--bsk", default=None, help="bsk CLI 路径")
    args = parser.parse_args()

    global BSK
    if args.bsk:
        BSK = args.bsk

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 城市解析
    city = config.get("city", "")
    if re.fullmatch(r"\d+", str(city)):
        city_code = str(city)
    else:
        city_code = resolve_city_code(args.session, city)
        if not city_code:
            print(f"[ERR] 无法解析城市 '{city}'，退出")
            sys.exit(1)

    all_jobs = {}
    seen = set()
    for kw in config.get("keywords", []):
        kw = kw.strip()
        if kw:
            crawl_keyword(args.session, kw, city_code, config, all_jobs, seen)

    if not all_jobs:
        print("[ERR] 没有抓到任何岗位，检查 bsk 连接和关键词")
        sys.exit(1)

    passed, filtered = filter_jobs(list(all_jobs.values()), config)

    out_dir = config.get("output_dir", ".")
    os.makedirs(out_dir, exist_ok=True)
    fname = f"智联_{config.get('job_title', '岗位')}_{config.get('city', '')}_薪资{config.get('salary_min_k', 0)}K.html"
    output_path = os.path.join(out_dir, fname)
    generate_html(passed, filtered, len(all_jobs), config, output_path)


if __name__ == "__main__":
    main()
