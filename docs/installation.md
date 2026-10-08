# Windows + Codex 项目安装

v0.0.1 是开发预览，仅安装到你明确指定的项目。Windows x64 与 Python 3.14 是已选择的运行路线。真实发布验收范围见 Release notes。

## 两种安装入口

命令安装：使用仓库 README 中的固定版本 PowerShell 命令。脚本校验发布 ZIP 的 SHA256 和包内文件清单，再调用同一个 Python 安装后端。已有兼容 Python 优先复用；否则用已有或项目私有的 uv 下载 Python 3.14，不改全局 PATH 或 Windows Python 注册表。

Agent 安装：将 GitHub 地址和目标项目交给 Agent，要求读取此文与安装 Skill。Agent 应调用 install.ps1，不另写安装程序。目标项目内已有同名 MCP/Skill 或未知配置时停止覆盖，并说明冲突。

安装前必须询问：是否启用附带的作者/个人审美观点？这些观点不是统一标准，也不代表你的偏好。本版只附两张原创基础示例，没有第三方观点卡；仍保留选择机制。未答复记录 unanswered，不启用可选材料，升级保持已保存选择。

目标项目不得放在任何 Eagle .library 内；无效图库和越界目标会在创建运行文件前被拒绝。依赖构建的临时文件与缓存也归目标项目所有，不写入发布源码目录。

图库、向 Agent 分享图片预览、Voyage 在线查询预算分别授权。不扫描其他库或凭据。缺少个人参考卡、图库、索引或云端配置时仍可使用基础知识示例。

## 命令参数

在下载到的 install.ps1 所在位置运行，例如：

```powershell
./install.ps1 -Project 'C:\Work\MyProject'
./install.ps1 -Project 'C:\Work\MyProject' -Aesthetics disable
./install.ps1 -Project 'C:\Work\MyProject' -Action status
```

-Aesthetics 只接受明确回答的 enable/disable。-NonInteractive 省略询问，首次仍为 unanswered，更新沿用原选择。-Library 指定本次获准的 .library 目录；-AllowPreview 表示允许向 Agent 返回图片，不隐含付费 API 许可。已绑定其他图库时使用独立新项目，避免混用旧索引和维护记录。

离线或开发验收可提供 -PackagePath <带 RELEASE_MANIFEST.json 的包目录> 和 -Wheelhouse <锁定依赖 wheel 目录>。包清单不匹配时拒绝安装。正常安装从固定 Release URL 获取程序，不需要 Git 或 npm。

需要强制使用项目私有 Python 时可传 -ManagedPython；脚本使用 uv 的 --no-bin 和 --no-registry，避免注册全局命令或 Windows Python 配置。

## 项目布局

- .local/app/releases/<摘要>/：独立虚拟环境、wheel 和构建快照。
- .local/workspace.json、.local/data/cards/、.local/runtime/：本实例配置、卡库和索引。
- .local/install/receipt.json：安装状态、选择、资源归属与当前/前一程序版本。
- .local/install/install-v0.0.1.ps1：保留的管理入口，离线时可用于卸载/恢复。
- .agents/skills/paa-public/：Skill、方法、词表和按选择安装的资源。
- .codex/config.toml 的 paa_public：仅项目级 MCP，使用独立解释器与明确工作区。

不要把整个 .local 视为可删除目录；其中可能包含用户数据或其他工具的文件。安装器不改变用户全局 Codex 配置。在 Codex 中打开并信任该项目，按客户端需要刷新连接或重新打开；项目配置成功不等于现有聊天已经重连。

## 更新、卸载、恢复

再次运行 install 可更新；保持审美选择和用户卡。已修改/删除的种子不被静默覆盖或复活。升级资源发生同名冲突时保护文件并报告。包管理的可选种子关闭时移出运行库，编辑内容留在恢复区。

安装器在切换配置前检查候选解释器的依赖导入。旧程序和安装文件前后内容会保留。事务中断时运行入口拒绝部分状态，先确认旧进程结束，再续接：

```powershell
& 'C:\Work\MyProject\.local\install\install-v0.0.1.ps1' -Project 'C:\Work\MyProject' -Action recover
& 'C:\Work\MyProject\.local\install\install-v0.0.1.ps1' -Project 'C:\Work\MyProject' -Action rollback
& 'C:\Work\MyProject\.local\install\install-v0.0.1.ps1' -Project 'C:\Work\MyProject' -Action uninstall
```

rollback 只恢复前一可用程序/MCP 启动配置，保留当前卡库、审美选择和方法资源；没有前一版本会明确报错。它不是跨任意未来数据库格式的回退承诺。

uninstall 撤销本项目中归属明确的 MCP 和使用 Skill；改写过的 Skill 被保留在恢复区。默认保留程序版本、管理入口、图库、卡库、凭据和索引供恢复或重装。不删除其他工具配置，不自动清理用户数据。若 MCP 已被另行修改，会拒绝覆盖。

后续明确需要彻底清理时，先核对 receipt 和保留路径，再由当前用户授权。发生 writer 锁残留时先确认原进程已退出，不能盲删锁。

## 本机维护和费用

maintain run 默认仅本机；显式 --cloud 才检查本实例连接配置。新安装 maintenance_vectors=false，回执 not_requested 表示没有请求向量构建。启用向量前，明确素材、在线发送、预算与凭据；没有许可时不自动索引。

从目标项目执行安装记录中的 Python，参数使用 -I -X utf8 -m paa。-I 隔离模块路径，-X utf8 保证 Windows 中文输出。credential-set 使用隐藏输入，并将凭据加密保存在当前实例；不要把密钥发到聊天或写入仓库。

默认 allow_paid_api=false、query_online_default=false、budget_usd=0。预算是当前运行目录的累计保守用量；多实例的总费用需要统一规划。不开启 paid/online 时只使用缓存或关键词降级。API 成功不证明召回质量，须实际看图。

## 验证与边界

公共 CI 使用合成图片；检查安装、选择、恢复、用户数据保护、MCP 工具和图像字节。第三方图库、私人原图、测试密钥和模型账户会话不进入 CI。用户实际看到图片与自然语言同图追问，需要真实桌面验收。

官方位置参考：[Codex Skills](https://developers.openai.com/codex/skills)、[Codex MCP](https://developers.openai.com/codex/mcp)。当前项目级安装不移除用户已有全局工具；Agent 应核对 library_status 返回的工作区。
