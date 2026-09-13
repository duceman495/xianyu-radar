# xianyu-radar

[![CI](https://github.com/duceman495/xianyu-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/duceman495/xianyu-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-55%20passed-brightgreen.svg)](tests/)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-compatible-purple.svg)](SKILL.md)

闲鱼（goofish.com）行情雷达：**采集 → 存储 → 多维分析 → 生成报告**。

一个可独立运行的 CLI / Python 库，也可以作为 AI Agent Skill 使用。

> 为什么要做这个：闲鱼上"卖什么赚钱"的问题，靠感觉猜是没用的。
> 我做了这个工具，把 20+ 组关键词的真实在售数据抓下来，然后发现了一些
> 反直觉的结论——比如**加"AI"前缀反而让受众缩小 89%**，
> 比如**同一类服务 84% 挤在 10 元以下，但没人站住 10-30 元**。
> 数据不会骗人，感觉会。

---

## 它解决什么问题

| 你的问题 | 这个工具的回答 |
|---|---|
| "这个关键词在闲鱼有需求吗？" | 抓 60 条在售商品，看**想要数**中位数 |
| "我该定价多少？" | 看价格分布，找**供给少但需求强的价格带** |
| "是不是红海？" | 看商品同质化程度 + 头部集中度 |
| "谁在卖？卖得好吗？" | 看卖家维度、信用分布、批量铺货识别 |
| "最近变贵了还是便宜了？" | 多批次趋势对比，单商品价格轨迹 |

---

## 安装

```bash
git clone https://github.com/<you>/xianyu-radar.git
cd xianyu-radar

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # macOS/Linux
# .venv\Scripts\python -m pip install -r requirements.txt  # Windows

# 一次性扫码登录（本人操作，脚本不代填任何凭据）
.venv/bin/python -m xianyu_radar login
```

要求 Python **3.10+**。

国内网络加速：
```bash
.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 快速开始

```bash
V=.venv/bin/python

$V -m xianyu_radar doctor                    # 环境自检（只读，不联网）
$V -m xianyu_radar search "机械键盘" --pages 2
$V -m xianyu_radar analyze "机械键盘"         # 生成 Markdown 报告
$V -m xianyu_radar compare "关键词A,关键词B,关键词C"
```

输出示例见 [`docs/sample-report.md`](docs/sample-report.md)。

### 常用命令

| 命令 | 作用 |
|---|---|
| `doctor` | 只读自检：Python / 依赖 / 浏览器 / 登录态 |
| `login` | 扫码登录，保存 Cookie（约 7 天有效） |
| `search "kw" --pages N` | 抓取搜索结果，自动去重入库 |
| `analyze "kw"` | 生成行情报告（价格分布 / 供给结构 / 缺口） |
| `compare "a,b,c"` | 多关键词横向对比 |
| `trend "kw"` | 多批次中位价变化 |
| `history "标题或ID"` | 单商品跨批次价格轨迹 |
| `export --format csv` | 导出 CSV / JSON |
| `watch "kw"` | 周期性抓取，只报告新增 |

---

## 关于「机会分」

`analyze` 会给每个价格带打一个 **机会分**，用来找"供给少但需求强"的位置：

```
机会分 = 需求强度 / (供给量 + 1)
```

- **高分** = 有人在找，但没几个人在卖 → 值得进
- **低分** = 供给过剩或没人要 → 别碰

这不是玄学，就是**需求供给比**。它帮我发现了几个真实的空档，
也能帮你避开那些看着热闹、其实赚不到钱的坑。

---

## 作为 AI Agent Skill 使用

本仓库符合 [Agent Skills 规范](https://agentskills.io/specification)，
`SKILL.md` 在仓库根目录。把仓库目录放进你的 skill 目录即可：

```bash
ln -s "$PWD" ~/.pi/agent/skills/xianyu-radar
# 或 Claude Code:  cp -r . ~/.claude/skills/xianyu-radar
```

之后直接对 Agent 说「分析一下闲鱼上 XX 的行情」即可。

---

## 合规与边界

**这个工具做什么**
- 只读取**公开在售商品列表**：标题、价格、地区、想要数、卖家昵称
- 请求之间有间隔，不做高频轮询
- 登录态仅存本机（`~/.xianyu-data/storage_state.json`，权限 600）

**这个工具不做什么**
- 不采集、不存储任何**联系方式**（手机号 / 微信 / QQ）
- 不抓取用户隐私数据
- 不做自动私信、不批量发布
- 不绕过任何风控（登录由本人扫码完成）

请自行遵守闲鱼用户协议与当地法律。用于商业决策前，
建议只做**小样本、脱敏**的调研，不要规模化持续抓取。

---

## 开发

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

---

## 协议

MIT

## 致谢

接口形态参考了社区里若干 MIT 协议的闲鱼工具实现。
本项目的代码与核心分析引擎为独立实现。
