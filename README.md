# qqmusic-decrypt

QQ 音乐 QMC 加密音频**批量解密 + 歌单/我喜欢下载**工具（macOS / Windows 双平台，单文件、无第三方依赖，Python 3.9+）。

> ⚠️ **免责声明**：仅用于解密/下载**你本人账号有权限**、用于个人备份的音频。禁止绕过付费权限、
> 传播版权内容或商业使用；使用风险（含账号受限）自负。完整声明见 [DISCLAIMER.md](DISCLAIMER.md)。
> 原始 unlock-music 项目已于 2022-11 被腾讯 DMCA 下架。

**完整原理与操作记录见 [TECHNICAL.md](TECHNICAL.md)**（格式逆向、密码学细节、接口逆向、验证证据、踩坑记录）。

## 功能

### 本地解密

| 格式 | 密钥来源 | 状态 |
|---|---|---|
| `.mgg/.mflac` musicex（macOS 客户端 ≥ 19.57，文件尾 `musicex\0`） | `GetEVkey` API | ✅ 实测 |
| iMusic 缓存 `<song_id>-<type>.mgg/.mflac`（无 footer） | sqlite 元数据 + API（自动尝试 O8M0/O4M0/O6M0/M8M0、F0M0 前缀） | ✅ 实测 |
| QMC2 QTag / V1（`.mggl/.mflac/.mgg` 尾块内嵌 ekey） | 离线 | ✅ 自测 |
| QMC1 legacy（`.qmc0/.qmc2/.qmc3/.qmcflac/.qmcogg/...`） | 内嵌 key 或 Static 盒 | ✅ 自测 |
| ekey 包装 | EncV1（8B 头 + TC-TEA）与 EncV2（双 TEA 包装） | ✅ 自测 |

### 在线歌单 / 我喜欢 / 下载

| 能力 | 接口（均实测于当前账号） |
|---|---|
| 创建的歌单列表 | `music.musicasset.PlaylistBaseRead.GetPlaylistInfoDiff` |
| 收藏的歌单列表 | `music.musicasset.PlaylistFavRead.GetPlaylistInfoDiff` |
| 「我喜欢」(dirId=201) | 歌单列表自动定位 tid，再走详情接口 |
| 歌单详情/分页歌曲 | `music.srfDissInfo.DissInfo.CgiGetDiss`（`song_num=50` 分页） |
| 加密资源下载+解密 | `GetEVkey`（F0M0/O8M0/O6M0/O4M0）→ 下载 purl → QMC2 解密 |
| 普通音质兜底 | `CgiGetVkey`（C400/M500/M800）直接下载 |
| 音频标签 | ffmpeg 写入标题/歌手/专辑（可选） |

算法实现与验证：
- 腾讯魔改 TEA：16 轮、tweaked CBC（`PadLen+Padding+Salt+Body+Zero7` 帧），已通过
  [jixunmoe/tc_tea_rust](https://github.com/jixunmoe/tc_tea_rust) 的官方测试向量；
- QMC2 Map / RC4 变体（0x80 首段 + 0x1400 分段）、QMC1 Static 盒：内置自测往返；
- musicex footer 偏移为实测值（`+0xAC flag / +0xB0 size / +0xB4 version / +0xB8 magic`）。

## 快速开始

```bash
cd ~/project/qqmusic-decrypt

# 1. 交互式菜单（无参数直接进入；带参数仍是脚本化批处理）
python3 qqmusic_decrypt.py

# 2. 自测（不联网、不读写歌曲文件）
python3 qqmusic_decrypt.py --self-test

# 3. 看文件格式（不调 API）
python3 qqmusic_decrypt.py --info ~/Downloads

# 4. 预演整个本地库（默认扫描 ~/Downloads + QQ 音乐 iMusic 缓存库）
python3 qqmusic_decrypt.py --dry-run

# 5. 本地批量解密（输出默认 ~/Music/QQMusicDecrypted）
python3 qqmusic_decrypt.py ~/Downloads

# 6. 列出账号歌单
python3 qqmusic_decrypt.py --list-playlists

# 7. 下载「我喜欢」（先 --limit 试一首，再全量）
python3 qqmusic_decrypt.py --favorites --limit 1
python3 qqmusic_decrypt.py --favorites

# 8. 下载指定歌单（tid 来自第 6 步）
python3 qqmusic_decrypt.py --playlist 9028251943 --limit 1
python3 qqmusic_decrypt.py --playlist 9028251943 --playlist 8423995962

# 9. 只下载指定音质（默认 flac,320,m4a；可选 flac/320/192/128/m4a/mp3-128/mp3-320）
python3 qqmusic_decrypt.py --favorites --quality flac
```

## 常用参数

| 参数 | 说明 |
|---|---|
| （无参数） | 进入**交互式菜单**（歌单/我喜欢/本地解密/设置/自测） |
| `-i, --interactive` | 显式进入交互式菜单 |
| `paths` | 本地文件或目录；无远程参数且缺省时 = `~/Downloads` + `iMusic` 库 |
| `--list-playlists` | 列出创建歌单 + 收藏歌单（含 tid / 歌名数） |
| `--playlist TID` | 下载指定歌单（可重复） |
| `--favorites` | 下载「我喜欢」 |
| `--quality LIST` | 下载音质优先级，默认 `flac,320,m4a` |
| `--limit N` | 本地模式最多处理 N 个文件；歌单模式每歌单最多 N 首 |
| `--out-dir DIR` | 输出目录（默认 `~/Music/QQMusicDecrypted`；歌单按「歌单名/」分子目录） |
| `--in-place` | 本地模式输出到源文件同目录 |
| `--overwrite` | 覆盖已存在输出（默认 SKIP） |
| `--recursive` | 目录递归（iMusic 库默认自动递归） |
| `--dry-run` / `--info` | 预演 / 只看格式信息 |
| `--no-api` | 禁用 API（只解内嵌 ekey / 静态格式） |
| `--tag` / `--no-tag` | 是否用 ffmpeg 写标题/歌手/专辑（默认开，失败不致命） |
| `--no-ffprobe` | 不做 ffprobe 时长验证 |
| `--delay S` | API 调用间隔（默认 0.15s） |
| `--db / --prefs` | 覆盖 sqlite / plist 路径 |
| `--uin U --authst A` | 手动凭据（跳过自动凭据） |
| `--platform auto\|20\|27` | 强制 API platform（默认 auto：Win=27, Mac=20） |
| `--self-test` | 内置自测 |

## 凭据

- macOS：自动读取 `~/Library/Preferences/com.tencent.QQMusicMac.plist`
  （`AutoLoginUserInfo` NSKeyedArchiver → `nUserId` + `strAuthst`）。
- 手动：`--uin` + `--authst`。
- `authst` 会过期；批量中途收到 `code=500001` 会自动停止，重新登录 QQ 音乐后再跑。
- Windows 客户端请自行取 `uin`（`QQMusicServiceConfig.ini`）与运行中进程内存里的
  `authst`，再用 `--uin/--authst` 传入。

## 输出命名

- 有数据库元数据时：`<歌手> - <歌名>.<ogg|flac|mp3|m4a>`
- 无元数据时：源文件 stem
- 同名输出已存在 → 默认 SKIP；`--overwrite` 覆盖；扩展名由解密后真实签名决定（不盲信源后缀）

## Windows 兼容性（已实现，2026-08-18 实测）

- **自动检测平台并分支**：Win/Mac 共用同一脚本；`platform` API 参数自动取
  27（Win）/ 20（Mac），`--platform 20|27` 可强制；
- **Win 凭据**：`Uin` 读 `%APPDATA%\Tencent\QQMusic\QQMusicServiceConfig.ini`，
  `authst` 自动从运行中的 `QQMusic.exe` 内存提取（需客户端登录并运行；多个候选自动轮换）；
- **Win 缓存**：自动发现注册表 `CACHEPATH` → `downloadproxyNew\tp2p\.tpfs\duty\`，
  文件名 `O4M0xxx.mgg / F0M0xxx.mflac` 自带质量前缀，无需数据库即可解密；
- **Win 歌单接口**：自动切换 `GetPlaylistByUin` / `GetPlaylistFavInfo` /
  `DissInfoForPc.uniform_get_Dissinfo`（与 Mac 返回同构，实测歌单/我喜欢逐项一致）；
- 实测：Win 端 duty 缓存解密（227.7s OGG）、歌单列表、我喜欢 FLAC 下载全部成功；
- Win 控制台输出已统一 UTF-8；无 sqlite3 的 embeddable Python 也可运行（数据库仅 Mac 路径需要）；
- 详见 [TECHNICAL.md §11](TECHNICAL.md)。

## 发布包（内置 ffmpeg/ffprobe，无需系统安装）

| 包 | 内容 | 大小 |
|---|---|---|
| `release/qqmusic-decrypt-macos-arm64.zip` | macOS arm64 单文件 + 文档 | 23.6MB |
| `release/qqmusic-decrypt-win64.zip` | Windows x64 单文件 exe + 文档 | 180MB |

（校验和见 `release/SHA256SUMS.txt`）

- 单文件可执行：PyInstaller onefile，Python 运行时 + ffmpeg/ffprobe 全部内置；
  运行时优先使用内置二进制（`sys._MEIPASS`），仅当缺失才回退系统 PATH；
- 也可用环境变量 `QQMUSIC_FFMPEG` / `QQMUSIC_FFPROBE` 指定外部二进制；
- Win 包内置 Gyan ffmpeg 9.0 full_build（静态）；Mac 包内置 Homebrew ffmpeg 8.1 及全部依赖 dylib；
- 构建：Mac `PyInstaller --onefile --add-binary …`；Win 用 `qqmusic_decrypt_win.spec`
  （`FFMPEG_BIN` 环境变量指向 ffmpeg bin 目录）；
- 源码运行方式不受影响（`python3 qqmusic_decrypt.py` 仍可用）。

## 许可 / License

[GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0-or-later）。
使用、修改、分发本项目须遵守 AGPL-3.0；同时受 [DISCLAIMER.md](DISCLAIMER.md) 约束。

## 已知限制

- musicex / 加密下载依赖账号权限：`result=104003/104005` 表示该音质不可用，工具会自动降级下一音质（含普通 m4a 兜底）；
- 单个 `.mgg` 缓存可能需要最多 4 次 API 探测（O8M0→O4M0→O6M0→M8M0）；
- 歌单下载速度受 API 间隔与 CDN 带宽影响；`--quality flac` 单音质可减少 API 次数；
- 单线程；本地 24MB `.mflac` 约 6s，歌单每首含下载+解密+ffmpeg 标签；
- `.tkm` 等极老后缀未深度验证（已实现 Static/内嵌 key 路径，M4A 头修复未包含）；
- 歌单接口为当前 macOS 客户端（2026-08）实测参数，QQ 音乐改版后可能需要调整 module/method。

## 参考实现

- [unlock-music（Gitee 镜像）](https://gitee.com/ix64/unlock-music)：`qmc.ts / qmc_cipher.ts / qmc_key.ts`
- [jixunmoe/tc_tea_rust](https://github.com/jixunmoe/tc_tea_rust)：腾讯 TEA 权威实现与测试向量
- [pushbox/qmc2](https://github.com/pushbox/qmc2)：QMC2 C++ 实现（KeyDec / StreamCencrypt）
- [ownlight6/qmc-decoder](https://github.com/ownlight6/qmc-decoder)：musicex 格式与 GetEVkey 流程文档
- [ix64 MGG/MFLAC 研究 gist](https://gist.github.com/ix64/bcd72c151f21e1b050c9cc52d6ff27d5)
