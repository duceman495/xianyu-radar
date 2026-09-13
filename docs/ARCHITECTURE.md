# 架构说明

## 数据流

```
CLI (__main__.py)
      │
      ├── login.py ──► Playwright ──► 扫码 ──► storage_state.json
      │
      └── spider.py ──► mtop.py ──► h5api.m.goofish.com
                │                        │
                │                  签名 + Cookie
                │
                ▼
           parse_card()  ──► Item 对象
                │
                ▼
            store.py  ──► SQLite (batches / items / price_history)
                │
                ▼
          analyze.py  ──► Overview ──► render_report() ──► Markdown
```

## 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `mtop.py` | 签名、HTTP、错误分类 | 不含业务逻辑 |
| `spider.py` | 调用搜索接口、解析响应 | 不落盘 |
| `store.py` | SQLite 读写、去重、批次 | 不做分析 |
| `analyze.py` | 统计与洞察 | 不联网 |
| `login.py` | 扫码登录、凭据存储 | 不代填凭据 |
| `__main__.py` | 参数解析、I/O、编排 | 不做计算 |

依赖方向是单向的：`analyze` 不知道 `spider` 存在，`store` 不知道 `mtop` 存在。
所以分析逻辑可以拿任意来源的数据测试——这就是测试不需要联网的原因。

## 为什么解析层要写得这么啰嗦

闲鱼搜索接口返回的是**给前端渲染用的结构**，不是干净的 API：

```jsonc
{"data": {"item": {"main": {
  "clickParam": {"args": {
      "price": "2",              // 价格是字符串
      "wantNum": "",             // 经常是空的！
      "publishTime": "1775206850000",  // 毫秒时间戳
      "seller_id": "htSi0OD+..."  // 混淆过的 ID
  }},
  "exContent": {
      "richTitle": [              // 标题不是字段，是富文本数组
          {"type": "Image", "data": {"url": "..."}},
          {"type": "Text",  "data": {"text": "真正的标题"}}
      ],
      "fishTags": {               // 想要数和信用藏在"标签"里
          "r3": {"tagList": [{"data": {"content": "1371人想要"}}]},
          "r4": {"tagList": [{"data": {"content": "卖家信用极好"}}]}
      },
      "userNickName": "...",
      "area": "..."
  }
}}}}
```

关键结论：**想要数、信用等级这些核心指标，在结构化字段里经常是空的，
真正的值在展示文本里**。所以解析要分两层——

1. 先读结构化字段（`clickParam.args`）
2. 空了就回退到展示文本（`fishTags` 里的中文，用正则抠）

这个双层设计在 `parse_card()` 里体现为一路 `if not x: x = ...`。
看起来笨，但接口每次改版都只改一层，另一层还能兜住。

## 机会分的设计

```python
opportunity = demand / (supply + 1)
```

- 分子 `demand` 用**中位想要数**而不是总想要数：
  总想要会被单个爆款带偏，中位数更能代表"普通商品能拿到多少关注"
- 分母 `supply + 1` 避免除零；`+1` 也让"1 条供给"不会得到虚高的分

为什么要这个指标：**光看供给量或光看需求量都会误判**。
- 供给少但没人要 → 0 分（分子是 0）
- 需求大但供给 200 条 → 低分（分母大）
- 供给 2 条、需求 2877 → 959 分（真正的空档）

## 测试策略

55 个测试，全部**离线**，用 `make_card()` 合成响应：

- **分类规则**：每种供给类型的边界，特别是"两种特征都有"的情况
- **解析健壮性**：坏卡片跳过、结构变了要报错（不能静默返回空）
- **统计正确性**：分位数、异常值、铺货识别
- **存储语义**：去重、批次排序、时间戳唯一性

这些测试抓出过两个真 bug：
1. 同秒内创建批次会撞 ID（已改为微秒时间戳）
2. 旧结构 `cardData` 的平铺字段没被读取（已加兜底）

## 扩展点

想加新数据源或新指标，不用改分析层：

- **新数据源**：实现一个返回 `Item` 列表的类，喂给 `Store` 即可
- **新指标**：在 `analyze.py` 加字段到 `Overview`，在 `render_report` 加一节
- **新价格带**：`build_bands()` 接受自定义 `bands` 参数
