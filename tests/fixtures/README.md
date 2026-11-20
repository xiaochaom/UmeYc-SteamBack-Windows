`deck-custom-v1.zip` 与对应 JSON 由 UmeYc-SteamBack v1.4.5
（commit abb57ae）的原始 `steamback.cloud.pack_current` 在隔离目录生成。
Steam Account ID=123，App ID=42，两个文件含中文路径、二进制内容，
mtime 固定为 1700000000000000000 ns。无真实用户数据。

测试会读取这份 Deck 归档，再用 Windows 实现重打包，要求 manifest
逐字段相等，包含文件顺序、JSON 哈希规则、目录 key 与修改时间。
