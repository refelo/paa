# PAA

Windows + Codex 项目级图片与知识参考工具。**v0.0.1 是开发预览，尚不适合无人值守生产使用。**

PAA 结合 Eagle 当前文字信息与可选 Voyage 图像向量召回候选，由 Agent 实际看图、读上下文并判断用途。没有向量时明确降级为关键词检索；不把相似度当作条件成立或个人偏好的证明。

## 命令安装

在你选定的项目目录打开 PowerShell，执行：

```powershell
Invoke-WebRequest 'https://github.com/refelo/paa/releases/download/v0.0.1/install.ps1' -OutFile "$env:TEMP\paa-install.ps1"
& "$env:TEMP\paa-install.ps1" -Project $PWD.Path
```

脚本会询问是否启用作者观点，并将结果保存在项目内。未答复不会启用；本版没有附带第三方观点卡。已有 Python 3.14 会被复用；缺少时可下载项目私有 Python，不改全局 PATH。

若本机策略不允许运行下载的脚本，请先查看脚本，并按你自己的设备策略允许这次执行；项目不会修改全局执行策略。

## 让 Agent 安装

把下面的请求交给 Codex：

> 请读取 https://github.com/refelo/paa 的 README 和安装指引，将固定版本 v0.0.1 安装到我指定的项目，使用项目级 MCP/Skill，保留已有配置。请先询问是否启用作者审美内容，图库和在线费用另外确认。

安装后在 Codex 打开并信任该项目，重新发现其项目配置。先让 Agent 用 paa_public 的 library_status 核对工作区。方法入口在已安装 Skill 的 references/INDEX.md。

## 数据与费用

- 图库必须明确指定；不会自动寻找其他 Eagle/PAA 实例。没有图库也能查询随包知识示例。
- 默认不分享图片预览、不调用付费 API、不构建向量、不连接云端。图库、预览、在线预算由你分别授权。
- 普通检索不写回；维护只通过 Eagle 官方 API 改 annotation，保留人工前文及其他字段。
- 程序、索引、凭据和恢复文件在项目 .local 下，原图片仍在你指定的 Eagle 图库。
- 本版提供两张项目原创示例，CC BY 4.0；它们不是摄影课程汇编或你的审美画像。

## 更新、卸载和恢复

按 [安装指引](docs/installation.md) 使用同一脚本的 install、uninstall、recover、rollback、status 操作。卸载默认撤销项目接入并保留数据；不会删除原图库或其他工具。你也可以直接让 Agent 卸载，它应使用同一入口。

## 开发验证

```powershell
python scripts/check_isolated_tests.py
python -m ruff check paa scripts tests
```

公开 CI 仅用合成素材。SDK 返回图像字节与实际桌面看图是不同证据，已完成的验收见 Release notes。云端部署、其他系统、本地大模型和全局安装不在这个预览版范围内。

软件和原创方法文档使用 [MIT](LICENSE)，原创知识示例使用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。范围与依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
