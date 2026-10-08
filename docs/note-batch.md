# 图片备注批次与续接

方法用于明确获准的本实例图片范围，不自动启动维护。先读[图片方法](note-writing.md)和[词表](note-vocabulary.json)，查看当前安装资料选择；核心技巧可用，个人审美观点仅在明确启用时取用。

## 范围与分配

维护运行已冻结 ID 时使用其 notes 目录，不再另起范围。另行获准的新批次才初始化，示例参数须替换为真实已确认路径：

```powershell
python -m paa.note_batch init --run <本次私有运行目录> --library <已授权图库路径> --ids-file <明确图片ID数组文件>
python -m paa.note_batch status --run <本次私有运行目录>
python -m paa.note_batch prepare --run <本次私有运行目录> --worker <唯一执行者名> --size <本次批量>
```

`prepare` 返回当前图像与完整 context，任务以 input.json 的完整稳定 ID 为准。一个 ID 同时只有一个写者；中断保留原 worker，完成后新批使用新名称，不覆盖旧分配。并发只有当次明确安排才开启，模型和数量不写成永久默认。

执行者实际收到并看每张图，必要时查看多画格/代表帧；旧 AI 段不能代替观察。按五核心与充分自由观察写新正文，方法细节不要重复复制成派工提示的另一个版本。

```json
{
  "viewed_ids": ["本次取图返回的完整asset_id"],
  "drafts": [{"asset_id": "同一个完整asset_id", "body": "实际看图后的新正文，不包含AI分隔符"}]
}
```

声明不是看图证明；输入不可读或草稿有缺项如实报告，不补造 viewed_ids。长材料截断后补读，压缩后补回丢失的方法；复用仍有效观察，不重做已完成项。

## 写回与恢复

在已授权的写入范围内执行 `python -m paa.note_batch apply --run <运行目录> --worker <执行者> --apply`。只通过 Eagle 官方 annotation 接口，保留人工前文和其他字段；不要同时启动另一批次写相同图片。只读预览不等于写入许可。

以 progress.json 的逐项回执为完成依据，不用草稿数或 prepare 返回空冒充整轮完成。正在写入或 `write_uncertain` 时先等原进程结束；确认中断后用 `reconcile` 只读核对已写结果，不盲目重复 apply。需重写时再次 prepare 同一 worker，实际看当前图，不沿用失效草稿；文件问题解决后可用 `retry --id <Eagle_ID>` 重新纳入待处理。

遗留锁先核对对应进程是否结束；不抢仍在使用的锁。结束说明冻结范围、已完成、问题及未验证部分，操作问题为零不代表图像分析绝无误差。不建立全库备注副本、评分平台或自动个人画像。
