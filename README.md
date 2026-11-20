# UmeYc-SteamBack-Windows

Windows 游戏存档管理工具，通过夸克网盘与 [UmeYc-SteamBack Steam Deck 插件](https://github.com/xiaochaom/UmeYc-SteamBack)互通。

![云存档](src/umeyc_steamback/assets/cloud-save.svg)

## 功能

- 中文界面，识别 Steam 多个游戏库与账号；可以手动填写 Steam App ID。
- 夸克 App 扫码登录，显示昵称及用户 ID。Windows 会话使用当前用户 DPAPI 加密。
- 候选路径识别、手动选择存档文件/目录、游戏保存前后的文件变化嗅探；无需游戏支持 Steam 云。
- 手动备份到本地，可重命名、恢复、删除。备份持续保留，没有固定或滚动删除机制。
- 本地上传夸克、夸克同步到本地，展示本地存档时间、云端存档时间及上传时间。
- 每游戏可开启“关闭游戏后自动上传”，默认关闭，需要本程序保持运行。
- 云端每个 Steam 账号、每个游戏仅保留最新一份；发布成功后才清理旧版本。
- 下载校验 SHA-256、文件清单、Steam 账号与游戏 ID；覆盖前持久保存本地备份。

## Windows 运行

支持 Windows 10/11 x64。初版尚需真实账号和游戏联调；跨平台游戏本身的存档格式必须兼容。

打开仓库 [Releases](https://github.com/xiaochaom/UmeYc-SteamBack-Windows/releases/latest)，
下载 `UmeYc-SteamBack-Windows.zip`，解压并保持完整目录结构，运行
`UmeYc-SteamBack-Windows.exe`。不需要安装 Python；`SHA256.txt` 可用于校验下载文件。
程序未签名。主分支构建同时在 [Actions](https://github.com/xiaochaom/UmeYc-SteamBack-Windows/actions)
提供 30 天有效的调试制品。只有推送版本标签才会在所有 Windows 检查通过后发布 Release。

源码运行（Python 3.11/3.12）：

```powershell
git clone git@github.com:xiaochaom/UmeYc-SteamBack-Windows.git
cd UmeYc-SteamBack-Windows
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m umeyc_steamback
```

## 第一次与 Deck 互通

1. 在 Deck 上传该游戏存档，Windows 扫码登录同一个夸克账号。
2. 选择同一个 Steam 账号和游戏 App ID。账号栏使用 **32 位 Account ID**；“填写账号 ID”也接受 SteamID64 并转换。
3. Windows 点击“云端同步到本地”。程序校验云存档，并逐项展示 Deck 的目录标识。
4. 为每个云端目录选择 Windows 对应的存档文件夹。例如 Deck 的 `proton/drive_c/users/steamuser/AppData/Local/某游戏/Saved` 对应 Windows 的 `%LOCALAPPDATA%\某游戏\Saved`。
5. 确认时间与目录后同步。后续 Windows 上传会沿用这些目录标识和相对文件名，Deck 可继续读取。

**优先从已有存档的一端上传，再在另一端建立目录对应。** 如果 Windows 先上传，两端都手动选路径时应选择同一层级、同样的目录顺序，Deck 使用手动路径模式。Windows 对应文件夹可为空，但需先创建并明确选择。选择单个文件时，同一父目录会合并为一个目录槽位。

重新“选择存档路径”会重置为手动路径模式。若 Deck 先前使用自动路径模式，请再次从云端同步，确认目录对应，避免两端目录标识不一致。多个根目录必须一一对应，不能互相包含。程序不会根据云端绝对路径直接写文件。

## 找不到存档时

“识别候选路径”检查当前游戏的 Steam `userdata/.../remote` 和常见用户目录中与游戏名称/安装目录同名的位置，候选需要人工确认。它不是完整游戏存档数据库，无法保证识别任意游戏。

“开始嗅探”记录用户 Documents、Saved Games、AppData、该游戏安装目录和 Steam 账号目录中的文件状态。随后启动游戏、保存一次进度、退出，再点“完成嗅探”，勾选真实存档。缓存、配置也可能变化，所以嗅探不会自动采用路径或上传。单次扫描最多 10 万文件、20 秒；达到限制会提示，仍可手动选择路径。不会扫描整个磁盘。

## 备份、恢复与时间

应用数据及本地备份位于 `%LOCALAPPDATA%\UmeYc-SteamBack-Windows`，独立于游戏安装目录。卸载游戏不会删除这里的备份。不要将应用数据目录选为存档来源。

存档时间取所选文件中**最新的修改时间**；上传时间表示云端发布发生的时间。时间不同不一定表示进度不同，同步冲突还比较内容哈希。云端未知版本需要确认后才能覆盖，上传中发现另一台设备更新则停止发布。夸克没有提供本项目可用的跨设备原子锁，极窄的并发发布窗口仍可能产生多个索引，后续上传会按服务器时间保留最新版本。尽量避免两端同时上传同一个游戏。

从云端下载会保留“云端下载”和“同步前本地备份”；恢复本地记录会保存“恢复前本地备份”。恢复覆盖同名文件，保留额外的本地文件。恢复覆盖前备份可移除那次同步新建的文件。删除记录只删除备份，不删除游戏当前存档。

游戏运行时禁止备份、恢复和同步。检测依据 Steam 的运行状态与游戏安装目录中的进程；特殊启动器可能无法完全识别，请先退出游戏再操作。自动上传在检测到退出后等待一段时间；文件仍在变化时会停止并在窗口显示原因。没有后台服务，关闭程序后不会监控。

夸克目录沿用 `/UmeYc-SteamBack/<Steam Account ID>/<App ID>/`，使用 Deck `format=1` 清单及归档。清理只涉及该目录中符合插件文件命名规则的旧版本，不清空夸克回收站，也不会删除其他文件。

## 开发验证

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m umeyc_steamback --smoke-test
```

测试使用隔离目录和模拟网盘，不修改真实游戏存档。含 Deck v1.4.5 原始代码生成的格式兼容 fixture、同步冲突、失败保留旧版、恢复回滚、Windows 路径防护、界面重命名弹窗回归。Windows CI 额外验证 DPAPI、目录联接防护及打包 EXE 启动。扫码登录、真实夸克传输和实际游戏互通需要设备联调确认。

GPL-3.0-or-later，来源及依赖许可见 [NOTICE](NOTICE) 与 [LICENSE](LICENSE)。提交身份为 xiaochaom。
