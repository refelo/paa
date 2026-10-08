# 知识蒸馏 V2 接口

以下是继承代码已有的维护格式，需先完成本机安装与独立数据配置。它不是初始化空工作区的安装命令；阶段 A 不执行真实素材维护。

## 日常接续

统一命令为`python -m paa distill`，不增加MCP工具或新服务。

```powershell
python -m paa distill status
python -m paa distill sources --owner "<已登记的作者>" --limit 10
python -m paa distill show <source_id>
python -m paa distill show <source_id> --start <next_start>
python -m paa distill stage .local/outputs/batch.json
python -m paa distill queue
python -m paa distill catalog
python -m paa card-get <card_id>
python -m paa distill merge .local/outputs/merge.json
python -m paa distill merge .local/outputs/merge.json --apply
```

新增资料先通过`distill register <路径> [--collection 合集] --apply`登记；`sources`只列已登记且有效的未读资料，不自行扫描；`show`返回全部字幕文字的一段及下一段位置（SRT仅去掉序号/时间码，逐句文字保留；原文件仍完整归档），默认每段12000字符，可调`--size`。每段均应实际进入上下文，工具输出截断须补读。程序记录返回范围只能证明内容被返回，不能证明Agent读懂；`stage`同时要求全文读取声明，不能用声明替代补齐。

阅读阶段只暂存雏形，不检索或改写旧卡。雏形正文用自然语言写语境、实际建议与影响使用的边界，缺口按需要列出，不强套教程。`queue`返回未归并/备用内容，`catalog`只列活动旧卡的ID、标题、问题与条件提示；提示不完整时会标出`more_conditions`，必要时展开正文，不凭目录排除其他分支。归并时机由Agent判断，没有批量配额。

## 暂存格式

```json
{
  "batch_id": "B001",
  "full_text_read": true,
  "sources": [{"id": "D...", "decision": "read_pending_synthesis"}],
  "drafts": [{
    "id": "T001",
    "kind": "technique",
    "domain": "摄影",
    "title": "一句话说明用途",
    "content": "这份资料的语境、作者实际建议、适用边界和未能确定之处。",
    "gaps": [],
    "source_ids": ["D..."]
  }]
}
```

示例ID只是接续位置，必须使用当前清点中真实存在的ID。`decision`沿用已读状态：人物材料待综合用`read_pending_synthesis`，仅技巧材料用`distilled`，无可用知识用`no_useful_knowledge`，已核同源用`duplicate_reviewed`；它们不是整轮已完成声明。没有雏形可以用空数组，不强提卡。人物雏形用`preference`并提供`author`，须匹配已登记的明确作者；收藏夹不得生成人物卡。领域为摄影、绘画或跨媒介。

一个视频支持几个雏形时重复使用同一source_id即可，不填写引用范围。已知新字幕与此前某来源属于同视频时，可在source条目提供`same_video_as`。同批多个来源已确认同一期时可提供`video_groups: [["D1", "D2"]]`。工具不猜标题相似就是同视频；既有映射与新判断冲突会拒绝，先核对再进行明确的内部映射修正，不通过改计数绕过。

同一batch_id及相同内容重试不会重复暂存；不同内容复用ID会拒绝。原文原样归档，不进入正常召回；清点哈希不匹配、读取缺段或范围已排除时不推进状态。

## 归并格式

```json
{
  "queue_etag": "从status或queue取得的当前值",
  "operations": [{
    "target_id": "PT001",
    "expected_etag": "从card-get取得的当前值",
    "draft_ids": ["B001-T001"],
    "patch": {}
  }],
  "reserve": [],
  "synthesized_sources": []
}
```

以上draft_id仅示意，使用queue中实际存在的条目。`patch`只写有必要改变的正文；单纯增加提及次数可以留空。程序从旧卡、雏形与被吸收卡的内部视频集合取并集，不要求Agent携带ID长表。同一雏形可在一次归并中支持多张相关卡，但每张都应确实由它的资料支持；若不同来源只支持其中不同部分，暂存时分开写，避免把整组次数套给每个分支。归并后记录目标集合，不能跨次重复消费。同视频已计数且正文不变时不增加卡片修订。

新建卡用未占用的target_id与`expected_etag: null`，patch包含`kind`、`title`、`problem`、`method`、`conditions`、`limits`、`agent_notes`、`visual_dependency`；人物卡另有`author`、简短`support_scope`。前三种方法/条件/限制字段为非空字符串数组，agent_notes首行标注领域。ID、修订、状态和内部视频集合由程序补齐，不添加source_refs或手写mention_count。

吸收旧卡时在操作中加入`absorb: [{"id":"旧卡ID","etag":"当前值"}]`；旧卡可逆停用，正文与历史保留。相反做法是否在不同条件下合并，仍由Agent明确写入patch，程序不自动总结或化解冲突。禁止混合技巧与偏好，也不合并不同人物。未成熟条目放进reserve；已完成整个人物阶段的资料可通过synthesized_sources将原待综合状态结清，不能仅因有技巧进入卡库就结清人物综合。

不带`--apply`只预览会修改哪些卡、消费哪些雏形及去重后次数。在用户已经授权恢复及对应制作范围时，Agent自行检查并执行，不逐次请求确认。是否执行以当前使用者授权范围为准。


## 状态与恢复

本实例卡库中的 `qa/distillation-v2.json` 保存阅读与归并进度，`video-aliases.json` 用于本实例明确判定的同源关系。不能导入另一个私人实例的旧队列、快照或完成记录来假定本次已处理。

部分写入使普通读取拒绝半成品时，使用 `python -m paa distill recover`；先确认旧写入进程结束。恢复核对写前／写后内容，有其他编辑则停止，不覆盖。不得抢占仍在使用的锁。

卡片检索文本改变时旧向量失效，单纯支持次数变化可复用；索引更新需要对应的数据与费用许可。独立 MCP 通过不等于已打开的旧桌面连接更新。
