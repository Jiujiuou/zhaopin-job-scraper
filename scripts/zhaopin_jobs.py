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


# ============ HTML 报告（投递工作台版） ============
def generate_html(passed, filtered, total, config, output_path):
    """生成「投递工作台」交互式报告：紧凑网格 + 勾选 + localStorage 持久化 + 投递清单"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sort_by = config.get("sort_by", "salary")
    if sort_by == "hr":
        passed.sort(key=lambda j: (-j.get("hr_score", 0), -parse_salary_max(j.get("salary", ""))))
    else:
        passed.sort(key=lambda j: (-parse_salary_max(j.get("salary", "")), -j.get("hr_score", 0)))

    # ---- 构建前端 JOBS 数据 ----
    hr_map = {2: ("hr-active", "HR非常活跃"), 1: ("hr-mid", "HR活跃"), 0: ("hr-unknown", "HR未知")}
    jobs_data = []
    for j in passed:
        cls, label = hr_map.get(j.get("hr_score", 0), ("hr-unknown", "HR未知"))
        hr_text = f"{j.get('hr', '')} · {j.get('hr_reply', '回复时间未知')}".strip(" ·")
        jobs_data.append({
            "title": j.get("title", ""),
            "salary": j.get("salary", "面议"),
            "company": j.get("company", "(需确认)"),
            "area": j.get("area", ""),
            "hr_text": hr_text,
            "hr_cls": cls,
            "tags": [j.get("keyword", "")] if j.get("keyword") else [],
            "url": j.get("href", "#"),
        })
    jobs_json = json.dumps(jobs_data, ensure_ascii=False)
    total_jobs = len(jobs_data)

    # ---- 过滤列表（前30条） ----
    filtered_html = ""
    for fj in filtered[:30]:
        reasons_str = " | ".join(fj["reasons"])
        filtered_html += f'''
    <li class="filtered-item">
      <span>{fj.get("title", "?")} · {fj.get("company") or "?"} · {fj.get("salary") or "?"}</span>
      <span class="filter-reason">{reasons_str}</span>
    </li>'''

    title = f'{config.get("job_title", "岗位")} | {config.get("city", "")} | 薪资≥{config.get("salary_min_k", 0)}K'

    tpl = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>投递工作台 | {{TITLE}}</title>
<style>
  :root {
    --bg:#f5f6f8; --surface:#fff; --text:#1a1a2e; --text2:#6b7280;
    --accent:#2563eb; --accent-soft:#eff6ff; --accent-border:#93c5fd;
    --salary:#dc2626; --salary-soft:#fef2f2;
    --ok:#059669; --ok-soft:#ecfdf5; --warn:#d97706; --warn-soft:#fffbeb;
    --border:#e5e7eb; --radius:6px;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:var(--font); background:var(--bg); color:var(--text); font-size:13px; }
  .toolbar {
    position:sticky; top:0; z-index:50;
    background:rgba(255,255,255,.96); backdrop-filter:blur(6px);
    border-bottom:1px solid var(--border);
    padding:8px 14px;
    display:flex; align-items:center; gap:8px; flex-wrap:wrap;
    box-shadow:0 1px 4px rgba(0,0,0,.04);
  }
  .toolbar .title { font-weight:700; font-size:14px; margin-right:6px; white-space:nowrap; }
  .toolbar .stat { font-size:12px; color:var(--text2); white-space:nowrap; }
  .toolbar .stat b { color:var(--accent); }
  .toolbar .spacer { flex:1; }
  .btn {
    border:1px solid var(--border); background:var(--surface); color:var(--text);
    padding:4px 10px; border-radius:5px; font-size:12px; cursor:pointer; white-space:nowrap;
    transition:all .15s;
  }
  .btn:hover { border-color:var(--accent); color:var(--accent); }
  .btn.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  .btn.primary:hover { background:#1d4ed8; color:#fff; }
  .btn.ghost-active { background:var(--accent-soft); border-color:var(--accent-border); color:var(--accent); }
  .btn.small { padding:2px 8px; font-size:11px; }
  .wrap { padding:10px 14px 24px; }
  .sync-tip {
    background:var(--ok-soft); border:1px solid #a7f3d0; color:#047857;
    border-radius:6px; padding:7px 12px; font-size:12px; margin-bottom:10px;
  }
  .job-grid {
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(252px,1fr));
    gap:8px;
  }
  .job-card {
    position:relative;
    background:var(--surface); border:1.5px solid var(--border); border-radius:var(--radius);
    padding:8px 10px; cursor:pointer; user-select:none;
    transition:border-color .12s, background .12s, box-shadow .12s;
    display:flex; flex-direction:column; gap:4px;
  }
  .job-card:hover { border-color:var(--accent-border); box-shadow:0 2px 6px rgba(37,99,235,.08); }
  .job-card.selected {
    border-color:var(--accent); background:var(--accent-soft);
    box-shadow:0 0 0 1px var(--accent) inset;
  }
  .job-card .check-mark {
    position:absolute; top:-7px; left:-7px; width:18px; height:18px;
    background:var(--accent); color:#fff; border-radius:50%;
    font-size:11px; font-weight:700; display:none; align-items:center; justify-content:center;
    box-shadow:0 1px 3px rgba(0,0,0,.25);
  }
  .job-card.selected .check-mark { display:flex; }
  .card-row1 { display:flex; justify-content:space-between; align-items:baseline; gap:6px; }
  .job-title { font-size:12.5px; font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .salary { font-size:12px; font-weight:800; color:var(--salary); background:var(--salary-soft); padding:1px 6px; border-radius:4px; white-space:nowrap; }
  .company-row { display:flex; gap:6px; align-items:baseline; font-size:11px; color:var(--text2); min-width:0; }
  .company-name { font-weight:500; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .area-name { flex-shrink:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:45%; }
  .tags-row { display:flex; gap:4px; flex-wrap:wrap; }
  .tag { font-size:10px; padding:0 6px; border-radius:3px; background:#f3f4f6; color:var(--text2); }
  .card-footer { display:flex; justify-content:space-between; align-items:center; gap:6px; padding-top:5px; border-top:1px dashed var(--border); min-width:0; }
  .hr-badge { font-size:10px; padding:1px 6px; border-radius:3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; min-width:0; }
  .hr-active { background:var(--ok-soft); color:var(--ok); }
  .hr-mid { background:var(--warn-soft); color:var(--warn); }
  .hr-unknown { background:#f3f4f6; color:var(--text2); }
  .view-btn {
    flex-shrink:0; padding:2px 10px; font-size:11px; font-weight:600;
    color:#fff; background:var(--text); border:none; border-radius:4px; cursor:pointer; text-decoration:none;
    pointer-events:auto; transition:background .15s;
  }
  .view-btn:hover { background:var(--accent); }
  .empty { text-align:center; color:var(--text2); padding:48px 0; font-size:13px; }
  .apply-list { display:none; }
  .apply-list .list-head { font-size:13px; font-weight:600; margin:6px 0 10px; }
  .apply-item {
    display:flex; align-items:center; gap:10px; background:var(--surface);
    border:1px solid var(--border); border-radius:5px; padding:6px 10px; margin-bottom:5px;
  }
  .apply-item .idx { color:var(--text2); font-size:11px; min-width:26px; text-align:right; }
  .apply-item .ai-title { flex:1; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .apply-item .ai-company { color:var(--text2); font-size:11px; max-width:30%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .apply-item a { font-size:11px; color:var(--accent); text-decoration:none; flex-shrink:0; }
  .apply-item a:hover { text-decoration:underline; }
  .list-note { background:var(--accent-soft); border:1px solid var(--accent-border); border-radius:5px; padding:8px 12px; font-size:12px; color:#1e40af; margin-bottom:12px; }
  .filtered-section { background:#fff8f8; border:1px solid #fecaca; border-radius:var(--radius); padding:12px 14px; margin-top:20px; }
  .filtered-list { list-style:none; margin-top:8px; }
  .filtered-item { padding:4px 0; border-bottom:1px solid #fecaca; font-size:12px; display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .filter-reason { font-size:10px; padding:1px 6px; background:#dc2626; color:#fff; border-radius:4px; white-space:nowrap; flex-shrink:0; }
  @media (max-width:900px) { .job-grid { grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); } }
</style>
</head>
<body>
<div class="toolbar">
  <span class="title">🎯 {{TITLE}}</span>
  <span class="stat">共 <b id="statTotal">{{TOTAL}}</b> 个 · 已选 <b id="statSel">0</b></span>
  <span class="spacer"></span>
  <button class="btn small" id="btnAll">全选</button>
  <button class="btn small" id="btnInvert">反选</button>
  <button class="btn small" id="btnNone">清空</button>
  <button class="btn small" id="btnSortSalary">薪资 ↓</button>
  <button class="btn small" id="btnSortHr">活跃度 ↓</button>
  <button class="btn small ghost-active" id="btnSelOnly" title="只显示已选岗位">仅看已选</button>
  <button class="btn small" id="btnExport" title="下载选中岗位的 JSON 清单">导出 JSON</button>
  <button class="btn primary" id="btnApply">📋 生成投递清单</button>
</div>

<div class="wrap">
  <div class="sync-tip">💾 勾选状态自动保存到浏览器本地 —— 告诉 AI「开始投递我勾选的岗位」即可，无需导出。</div>
  <div id="gridView">
    <div class="job-grid" id="jobGrid"></div>
    <div class="empty" id="emptyTip" style="display:none">没有匹配的岗位，换个筛选条件试试。</div>
  </div>

  <div id="applyView" class="apply-list">
    <div class="list-note">💡 投递清单已生成：AI 将按下列顺序打开每个岗位详情 → 点击「立即投递」→ 记录结果。投递前请确认浏览器已登录智联招聘。</div>
    <div class="list-head" id="applyCount"></div>
    <div id="applyItems"></div>
    <button class="btn" id="btnBack">← 返回岗位网格</button>
  </div>

  <div class="filtered-section">
    <h3 class="section-title" style="font-size:13px;margin-bottom:4px;">❌ 已过滤岗位（前30条）</h3>
    <ul class="filtered-list">{{FILTERED}}</ul>
  </div>
</div>

<script>
const JOBS = {{DATA}};
// 勾选状态通过 localStorage 持久化：AI 可在自己的窗口打开本页后
// evaluate `localStorage.getItem('zp_apply_list')` 直接读取用户勾选的完整投递清单
let selected = new Set();
try {
  const saved = JSON.parse(localStorage.getItem('zp_selected') || '[]');
  if (Array.isArray(saved)) selected = new Set(saved.filter(i => i >= 0 && i < JOBS.length));
} catch(e) {}
let sortKey = 'default';
let selOnly = false;

const gridEl = document.getElementById('jobGrid');
const statSel = document.getElementById('statSel');

function saveState(){
  try {
    localStorage.setItem('zp_selected', JSON.stringify(Array.from(selected)));
    const items = JOBS.filter((_,i)=>selected.has(i)).map(j=>({title:j.title,company:j.company,salary:j.salary,area:j.area,url:j.url}));
    localStorage.setItem('zp_apply_list', JSON.stringify({
      generated_at: new Date().toLocaleString(),
      count: items.length,
      jobs: items
    }));
  } catch(e) {}
}

function salaryMax(s){
  if(!s || s.includes('面议')) return -1;
  if(s.includes('万')){
    const m = s.match(/([\d.]+)\s*[-~至]\s*([\d.]+)/);
    if(m) return parseFloat(m[2]) * 10;
    const single = s.match(/([\d.]+)/);
    return single ? parseFloat(single[0]) * 10 : 0;
  }
  const m = s.match(/([\d,]+)\s*[-~至]\s*([\d,]+)/);
  if(m) return parseFloat(m[2].replace(/,/g,'')) / 1000;
  const single = s.match(/([\d,]+)/);
  return single ? parseFloat(single[0].replace(/,/g,'')) / 1000 : 0;
}
function hrScore(c){
  return c==='hr-active' ? 2 : (c==='hr-mid' ? 1 : 0);
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function getOrdered(){
  let arr = JOBS.map((j,i)=>({j,i}));
  if(sortKey==='salary') arr.sort((a,b)=>salaryMax(b.j.salary)-salaryMax(a.j.salary));
  if(sortKey==='hr') arr.sort((a,b)=>hrScore(b.j.hr_cls)-hrScore(a.j.hr_cls));
  return arr;
}

function render(){
  gridEl.innerHTML = '';
  let shown = 0;
  for(const {j,i} of getOrdered()){
    if(selOnly && !selected.has(i)) continue;
    shown++;
    const card = document.createElement('div');
    card.className = 'job-card' + (selected.has(i) ? ' selected' : '');
    card.dataset.idx = i;
    card.innerHTML =
      '<span class="check-mark">✓</span>' +
      '<div class="card-row1"><span class="job-title" title="'+esc(j.title)+'">'+esc(j.title)+'</span>' +
      '<span class="salary">'+esc(j.salary)+'</span></div>' +
      '<div class="company-row"><span class="company-name" title="'+esc(j.company)+'">'+esc(j.company)+'</span>' +
      (j.area ? '<span class="area-name">'+esc(j.area)+'</span>' : '') + '</div>' +
      '<div class="tags-row">'+j.tags.map(t=>'<span class="tag">'+esc(t)+'</span>').join('')+'</div>' +
      '<div class="card-footer">' +
        '<span class="hr-badge '+esc(j.hr_cls)+'">'+esc(j.hr_text)+'</span>' +
        '<a class="view-btn" href="'+esc(j.url)+'" target="_blank" rel="noopener" onclick="event.stopPropagation()">详情 →</a>' +
      '</div>';
    card.addEventListener('click', ()=>toggle(i));
    gridEl.appendChild(card);
  }
  document.getElementById('emptyTip').style.display = shown ? 'none' : 'block';
  statSel.textContent = selected.size;
  document.getElementById('statTotal').textContent = JOBS.length;
}

function toggle(i){
  if(selected.has(i)) selected.delete(i); else selected.add(i);
  saveState();
  render();
}
function selectAll(){ JOBS.forEach((_,i)=>selected.add(i)); saveState(); render(); }
function invert(){ for(let i=0;i<JOBS.length;i++){ if(selected.has(i)) selected.delete(i); else selected.add(i);} saveState(); render(); }
function clearAll(){ selected.clear(); saveState(); render(); }
function sortBy(k){ sortKey = (sortKey===k ? 'default' : k); render(); }

document.getElementById('btnAll').onclick = selectAll;
document.getElementById('btnInvert').onclick = invert;
document.getElementById('btnNone').onclick = clearAll;
document.getElementById('btnSortSalary').onclick = ()=>sortBy('salary');
document.getElementById('btnSortHr').onclick = ()=>sortBy('hr');
document.getElementById('btnSelOnly').onclick = function(){
  selOnly = !selOnly;
  this.classList.toggle('ghost-active', selOnly);
  render();
};
document.getElementById('btnExport').onclick = function(){
  const items = JOBS.filter((_,i)=>selected.has(i)).map(j=>({title:j.title,company:j.company,salary:j.salary,url:j.url}));
  const blob = new Blob([JSON.stringify({generated_at:new Date().toLocaleString(),count:items.length,jobs:items},null,2)],{type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'zhaopin_apply_list.json';
  a.click();
  URL.revokeObjectURL(a.href);
};
document.getElementById('btnApply').onclick = function(){
  const items = JOBS.filter((_,i)=>selected.has(i));
  document.getElementById('applyCount').textContent = '已选 ' + items.length + ' 个岗位，按以下顺序投递：';
  document.getElementById('applyItems').innerHTML = items.map((j,idx)=>
    '<div class="apply-item">' +
      '<span class="idx">'+(idx+1)+'</span>' +
      '<span class="ai-title">'+esc(j.title)+'</span>' +
      '<span class="ai-company">'+esc(j.company)+'</span>' +
      '<a href="'+esc(j.url)+'" target="_blank" rel="noopener">打开 →</a>' +
    '</div>').join('');
  document.getElementById('gridView').style.display = 'none';
  document.getElementById('applyView').style.display = 'block';
  window.scrollTo(0,0);
};
document.getElementById('btnBack').onclick = function(){
  document.getElementById('applyView').style.display = 'none';
  document.getElementById('gridView').style.display = 'block';
};

render();
</script>
</body>
</html>
"""
    html = tpl.replace("{{TITLE}}", title) \
              .replace("{{TOTAL}}", str(total_jobs)) \
              .replace("{{DATA}}", jobs_json) \
              .replace("{{FILTERED}}", filtered_html or "<li>（无）</li>")

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
