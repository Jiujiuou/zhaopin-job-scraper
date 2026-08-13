# 🎯 zhaopin-job-scraper 智联招聘自动化

批量抓取智联招聘岗位，按薪资 / 城市 / 公司黑名单 / 岗位类型过滤，生成**可点击跳转的 HTML 报告**。

**岗位类型无关**：前端、后端、嵌入式、运营、产品……任何岗位都能用。只需要改一行配置。

![示例报告](docs/report_screenshot.png)

## ✨ 特点

- **免登录绕过**：用你自己的浏览器登录态（不是爬虫，不会触发验证码）
- **薪资直抓**：智联列表页直接显示薪资，无需逐个点进详情页，抓 500+ 岗位只要几分钟
- **4 种薪资格式**：自动识别「万 / K / 元 / 面议」并统一换算过滤
- **岗位无关**：包含词 / 排除词 / 黑名单全部可配置，任何职业都能用
- **零依赖**：纯 Python 标准库 + BrowserSkill（bsk CLI），无第三方包

## 🚀 快速开始（5 分钟）

### 1. 安装 BrowserSkill（只需一次）

```bash
# 安装 bsk CLI（Windows 示例）
set BSK_INSTALL_DIR=D:\BrowserSkill\bin
# 在 Chrome 安装 browser-skill 扩展，并连接（popup 显示绿色）
```

> 详细安装见 [BrowserSkill 官方文档](https://github.com/browser-skill/browser-skill)。需要 `bsk --version` 可用。

### 2. 准备配置

复制模板，填三个必填项（岗位关键词、城市、薪资）：

```bash
cp example/config_template.json my_job.json
```

编辑 `my_job.json`：

```json
{
  "job_title": "嵌入式开发",
  "keywords": ["嵌入式", "嵌入式软件"],
  "city": "深圳",
  "salary_min_k": 15,
  "include_terms": ["嵌入式", "单片机", "驱动"],
  "exclude_terms": ["前端", "测试工程师", "销售"]
}
```

> 全部配置项见 [docs/配置说明.md](docs/配置说明.md)。**必填只有 4 项**：`job_title`、`keywords`、`city`、`salary_min_k`，其余全部可选。

### 3. 连接浏览器并运行

```bash
# 先让浏览器打开并登录智联招聘 www.zhaopin.com
bsk session start                    # 记下输出的 session id（如 abc1）
bsk tab list --session abc1 --scope user
bsk tab borrow <智联标签页id> --session abc1

# 运行抓取+筛选+生成报告
python scripts/zhaopin_jobs.py --config my_job.json --session abc1
```

完成后当前目录会出现 `智联_嵌入式开发_深圳_薪资15K.html`，浏览器打开即用，每个岗位卡片可点击跳转智联详情页。

### 4. 停止会话

```bash
bsk session stop abc1
```

## 📁 目录结构

```
zhaopin-job-scraper/
├── SKILL.md              # WorkBuddy skill 入口（AI 使用手册）
├── scripts/
│   └── zhaopin_jobs.py   # 主脚本：抓取+过滤+报告，一步完成
├── cities.json           # 智联城市代码表（260+ 城市，已验证）
├── example/              # 示例配置（前端/嵌入式/运营/模板）
├── docs/
│   └── 配置说明.md         # 配置项详解
└── assets/
    └── report_template.html  # 报告模板（可自定义样式）
```

## ⚙️ 配置项速查

| 配置 | 必填 | 说明 | 默认 |
|------|------|------|------|
| `job_title` | ✅ | 报告标题（如 前端开发） | - |
| `keywords` | ✅ | 智联搜索关键词，可多个 | - |
| `city` | ✅ | 城市名（中文）或 jl 编码 | - |
| `salary_min_k` | ✅ | 薪资下限（K），0=不限 | 0 |
| `salary_max_k` | ❌ | 薪资上限（K） | 100 |
| `blacklist` | ❌ | 公司黑名单（模糊匹配） | [] |
| `include_terms` | ❌ | 标题必须包含词（任一命中） | [] |
| `exclude_terms` | ❌ | 标题排除词（任一命中即排除） | [] |
| `dispatch_terms` | ❌ | 派遣/外包排除词 | ["派遣"] |
| `hr_active_days` | ❌ | HR 活跃阈值（天） | 7 |
| `max_pages` | ❌ | 每关键词最大翻页数 | 12 |
| `sort_by` | ❌ | `salary` 薪资优先 / `hr` 活跃优先 | salary |
| `output_dir` | ❌ | 报告输出目录 | . |

## 🛠️ 内置岗位示例

| 画像 | 配置文件 | 说明 |
|------|---------|------|
| 前端开发 | `example/config_frontend.json` | 前端/web/vue/react，20K+ |
| 嵌入式开发 | `example/config_embedded.json` | 嵌入式/单片机/驱动，15K+ |
| 运营 | `example/config_operations.json` | 产品/内容/用户运营，10K+ |

复制任一配置改改关键词和薪资就是你的岗位画像。

## ❓ 常见问题

**Q: 提示 "session not registered"？**
A: bsk 会话空闲超时了，重新 `bsk session start` 再运行。

**Q: 抓到的岗位和智联搜索不一致？**
A: 智联搜索是全文模糊匹配，会混入描述里含关键词的岗位。用 `exclude_terms` 排除，或调大 `salary_min_k`。

**Q: 某个城市报错？**
A: 检查 `cities.json` 是否有该城市，没有就填 jl 编码（在智联网页搜索后从 URL 里看 `jl765` 这种数字）。

**Q: 智联改版后抓不到数据了？**
A: 智联 DOM 结构变化会导致选择器失效。检查 `scripts/zhaopin_jobs.py` 顶部的 `CARD_SELECTOR` 等常量，按新版页面更新即可。欢迎提 Issue 反馈。

## 📌 合规声明

- 本工具**仅用于个人求职**，使用你自己的浏览器登录态，不绕过任何验证机制
- 脚本内置 1-3 秒抓取间隔，请勿调小，尊重智联服务条款
- 数据版权归智联招聘所有，请勿商用或二次分发

## 📄 License

MIT

---

**最后验证日期**：2026-08-13（智联页面结构 + 260 城市代码 + 前端/嵌入式/运营三画像实测）
