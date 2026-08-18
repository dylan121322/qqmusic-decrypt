# QQ 音乐加密音频：格式逆向、解密与歌单下载 —— 技术文档

> 版本：2026-08-17（同日全部实测）｜设备：Mac 主开发机（本机实测）
> SPDX-License-Identifier: AGPL-3.0-or-later
> 工具位置：`<项目目录>/qqmusic-decrypt/qqmusic_decrypt.py`
> 配套：`README.md`（用户向快速开始）、本文档（技术向全记录）

---

## 0. 范围与声明

本文档完整记录以下工作的**操作过程与原理**：

1. QQ 音乐加密音频格式（QMC1 / QMC2 / musicex / iMusic 缓存）的逆向分析；
2. 腾讯魔改 TEA、QMC2 Map/RC4 变体、QMC1 Static 盒的算法还原与 Python 移植；
3. macOS 客户端登录凭据（uin/authst）的提取；
4. `GetEVkey` 加密密钥（ekey）获取与在线加密资源下载；
5. 歌单列表、「我喜欢」、歌单详情的接口逆向与批量下载实现；
6. 全链路验证证据、踩坑与修复记录。

**法律声明**：本文仅用于研究你本人账号合法获取、个人备份的音频；禁止用于绕过付费权限、
传播版权内容或商业用途，使用风险自负（详见仓库根目录 `DISCLAIMER.md`）。
加密方案为腾讯商业 DRM；原始 unlock-music 项目已于 2022-11-04 被腾讯 DMCA 下架
（[DMCA 通知](https://github.com/github/dmca/blob/master/2022/11/2022-11-04-qqmusic.md)）。

---

## 1. 成果总览

| 交付物 | 位置 |
|---|---|
| 解密/下载一体化 CLI | `<项目目录>/qqmusic-decrypt/qqmusic_decrypt.py`（约 1400 行，stdlib-only） |
| 用户文档 | `<项目目录>/qqmusic-decrypt/README.md` |
| 本文档 | `<项目目录>/qqmusic-decrypt/TECHNICAL.md` |
| 实测解密产物 | `<输出目录>/`（OGG/FLAC/M4A 若干） |
| 调研中间产物 | `<临时目录>/qmc-ref/`（参考源码、API 响应样本、字符串表等，临时目录） |

功能矩阵：

| 能力 | 状态 |
|---|---|
| 本地 `.mgg/.mflac`（musicex）解密 | ✅ 实测 |
| iMusic 缓存（无 footer）解密 | ✅ 实测 |
| QMC2 QTag / V1（内嵌 ekey）解密 | ✅ 自测（无真实样本，合成样本往返） |
| QMC1 legacy（qmc0/qmc3/qmcflac 等） | ✅ 自测（合成样本往返 + 官方算法对照） |
| 创建歌单 / 收藏歌单列表 | ✅ 实测 |
| 「我喜欢」全量列表 | ✅ 实测（307 首） |
| 歌单详情分页 | ✅ 实测 |
| 加密资源多音质下载（flac/320/192/128）+ 普通音质兜底 | ✅ 实测 |
| 音频标签写入（ffmpeg） | ✅ 实测 |
| authst 失效自动停批、断点续传（SKIP）、dry-run/info | ✅ |

---

## 2. 样本取证与文件格式分析

### 2.1 样本获取

本机发现两类真实样本：

- 下载文件：`<下载目录>/<歌曲>.mgg`（2,653,932 字节）
- 客户端缓存库：`~/Library/Containers/com.tencent.QQMusicMac/…/iMusic/`
  下 80~97 个 `<song_id>-13.mgg/.mflac`（数量随客户端缓存清理变化）

对全部样本做批量指纹扫描：文件头 16 字节、尾 16 字节、熵分布、已知魔数检索
（`OggS`/`fLaC`/`ID3`/`QTag`/`musicex`/`QMC` 等）。

结论：

- 97 个缓存文件**全部没有** QTag/V1/musicex 尾部结构，头尾均为高熵密文 → 纯密文缓存，密钥不落盘；
- 1 个下载样本尾部有 `musicex\0` 魔数 → 新代格式。

### 2.2 musicex 样本逐字节解剖（实测）

文件总长 `0x287eec`（2,653,932）。结构：

```
[ QMC2 加密音频 0x287e2c 字节 ] + [ musicex footer 0xc0 (192) 字节 ]
```

尾部 hexdump（最后 0x100 字节，节选）：

```
00287e2c: ce 30 1d 14 02 00 00 00 02 00 00 00 30 00 30 00
00287e38: "001WZk3f29SpSn" (UTF-16LE)
...
00287e74: "O4M0001WZk3f29SpSn.mgg" (UTF-16LE)
...
00287ed8: 01 00 00 00
00287edc: c0 00 00 00
00287ee0: 01 00 00 00
00287ee4: 6d 75 73 69 63 65 78 00      "musicex\0"
```

footer 字段表（**实测值**，已与 ownlight6 实现代码互证；其流程文档中的尾部偏移
`+0xBC/+0xC0/+0xC4/+0xC8` 与实际不符，以本表为准）：

| 偏移 | 长度 | 类型 | 实测值 | 含义 |
|---|---|---|---|---|
| +0x00 | 4 | u32 LE | `0x141d30ce` = 337,457,358 | song_id |
| +0x04 | 4 | u32 LE | 2 | 音质类型 1 |
| +0x08 | 4 | u32 LE | 2 | 音质类型 2 |
| +0x0C | 60B 缓冲 | UTF-16LE | `001WZk3f29SpSn` | media_mid（=DB K_SONG_RESERVE1/8） |
| +0x48 | 68B 缓冲 | UTF-16LE | `O4M0001WZk3f29SpSn.mgg` | 资源文件名 |
| +0x8C | 32B | 0x00 | 001… | 填充 |
| +0xAC | 4 | u32 LE | 1 | 标志位 |
| +0xB0 | 4 | u32 LE | 0xC0 | footer_size |
| +0xB4 | 4 | u32 LE | 1 | version |
| +0xB8 | 8 | ASCII | `musicex\0` | 魔数 |

**数据库交叉验证**（`qqmusic.sqlite`）：

```sql
SELECT id,name,singer,file,filesize FROM SONGS WHERE id=337457358;
-- 337457358 | 悪魔の子 (恶魔之子) | ヒグチアイ (HiguchiAi)
-- | /iQmc/ヒグチアイ...mgg | 2653740
```

`filesize = 2653740 = 0x287e2c`，与「文件总长 − 0xc0 footer」**逐字节相等** → DB 记录的
filesize 就是密文音频长度。song_id、media_mid 全部命中。

### 2.3 各代际格式对照

| 代际 | 后缀 | 布局 | 密钥 |
|---|---|---|---|
| QMC1 legacy | `.qmc0 .qmc3 .qmcflac …` | `[密文][base64 key][u32LE len]`，或 `[密文][key,songid,ver][u32BE len]["QTag"]`，或全文件无尾 | 内嵌 / Static 盒 |
| QMC2 (2021) | `.mflac .mgg .mggl`（QTag 尾） | `[密文][704B base64 ekey][,元数据][00 00 02 CC]["QTag"]` | 内嵌，TEA 包装 |
| musicex (macOS ≥19.57 / Win ≥22.x) | `.mflac .mgg` | `[密文][0xc0 footer]` | **不内嵌**，`GetEVkey` API 实时获取 |
| iMusic 缓存 | `<song_id>-13.mgg/.mflac` | `[纯密文]` | 内存/API，不落盘 |

- QTag 尾检测（2021 版）：文件末 8 字节 = `00 00 02 CC 51 54 61 67`
  （前 4 字节 LE 值 `0x2CC` + 字符串 `QTag`）；EKey 固定 704 字符（base64 → 528B）。
- V1 尾检测：末 4 字节 LE u32 ∈ (0, 0x400] 即 key 长度；
  `audio_len = total - key_len - 4`，key 块 = `[audio_len, total-4)`。
- 无尾结构 + `.mgg/.mflac` → iMusic 缓存（密钥需 API + DB 元数据）。
- 无尾结构 + `.qmc*` 等 legacy 后缀 → Static 盒。

### 2.4 缓存文件与质量前缀

缓存文件没有 footer，API 又要求「质量前缀 + 资源ID」文件名。前缀映射（来源：
客户端 `CSongURL::getSongNamePrefixAndSuffix` 反编译 + API 实测）：

| 前缀 | 资源 | 加密扩展名 |
|---|---|---|
| `O8M0` | OGG ~320kbps（实测平均 304kbps） | `.mgg` |
| `O6M0` | OGG 中高品质（API 返回 ekey，未下载实测） | `.mgg` |
| `O4M0` | OGG 标准品质（实测样本平均 93kbps VBR） | `.mgg` |
| `M8M0` | MP3 320k 加密资源（本账号 API 返回 104003，无权限） | `.mgg` |
| `F0M0` | FLAC 无损 | `.mflac` |
| `C400/M500/M800` | 普通（非加密）M4A/MP3 | 原扩展名 |

工具对 `.mgg` 缓存按 `O8M0 → O4M0 → O6M0 → M8M0` 顺序探测，以「解密后出现
`OggS`/`fLaC`/`ID3` 等合法签名」为胜出条件；`.mflac` 直接试 `F0M0`。
实测：`391934297-13.mgg` 用 O8M0 命中（O4M0 解密签名失败）；`551010366-13.mflac` 用 F0M0 命中。

第三方文档中「密文以 `#!Qk` 魔术头开始」的说法**不成立**：98 个真实文件开头无一匹配，
全库唯一一次 `23 21 51 6b` 出现在某 60MB 文件中段，属随机碰撞。

---

## 3. 密码学原理（全部有实现 + 自测）

### 3.1 密钥包装链

API / 文件内嵌的 `ekey` 是一个 base64 字符串，可能有三层：

```
层0（EncV2，新版 API）:
  outer_b64 = base64("QQMusic EncV2,Key:" + blob)
  blob = TC_TEA_encrypt( TC_TEA_encrypt( base64(encV1_raw), K2 ), K1 )
  K1 = "386ZJY!@#*$%^&)("   (16B ASCII)
  K2 = "**#!(#$%&^a1cZ,T"   (16B ASCII)
  解密顺序：decrypt(blob, K1) → decrypt(结果, K2) → base64decode → encV1_raw

层1（EncV1，本任务实际样本）:
  encV1_raw = [8B header] + [0x208B TC_TEA 密文]   (共 0x210 = 528B)
  TEA 密钥 = 交错拼接(simpleMakeKey(106,8), header)
  header 本身作为最终流密钥的前 8 字节

层2（最终流密钥）:
  stream_key = header + TC_TEA_decrypt(body, tea_key)
  长度 ≤ 300 → QMC2 Map 密码
  长度 > 300 → QMC2 RC4 变体（本任务两个真实样本均为 512B → RC4）
```

解析实现要点（`parse_ekey`）：
- 若 base64 解码后不以 EncV2 前缀开头，走 EncV1；若 TC-TEA 零校验失败，
  按 ownlight6 经验回退为「API 原生密钥」（整段直接当 key 用）。
- 实测 ekey：704 字符 → 528B → 8B 头 + 520B TEA 密文 → 512B 流密钥。

### 3.2 腾讯魔改 TEA（“Coffee”，tweaked CBC）

权威参考：[jixunmoe/tc_tea_rust](https://github.com/jixunmoe/tc_tea_rust)（ownlight6 实际
依赖）、[TarsCpp tc_tea.cpp](https://github.com/TarsCloud/TarsCpp)（C++ 原生实现）、
[ix64 gist](https://gist.github.com/ix64/bcd72c151f21e1b050c9cc52d6ff27d5)。

与标准 TEA 的区别：
- **16 轮**（标准 32/64 轮），DELTA=0x9E3779B9，大端 u32；
- **tweaked CBC**：双 IV 链。设块密文 `c[i]`、明文 `p[i]`：
  ```
  加密: x = p ^ iv1 ; c = TEA_ECB_encrypt(x) ^ iv2 ; iv1,iv2 = c,x
  解密: x = TEA_ECB_decrypt(c ^ iv2) ; p = x ^ iv1  ; iv1,iv2 = c,x
  初始 iv1 = iv2 = 0
  ```
- 密文帧格式：`PadLen(1) | Padding(padLen 0..7) | Salt(2) | Body | Zero(7)`；
  总长必为 8 的倍数；解密后以尾部 7 字节全零做完整性校验（zero-check）。
- ECB 单块（u64 BE）：
  ```
  y,z = block>>32, block&0xffffffff ; sum = DELTA*16
  重复 16 次:
    z -= ((y<<4)+k2) ^ (y+sum) ^ (y>>5)+k3   (模 2^32)
    y -= ((z<<4)+k0) ^ (z+sum) ^ (z>>5)+k1
    sum -= DELTA
  ```

**自测向量**（来自 tc_tea_rust 官方测试，全部通过）：
- ECB：key=`01 02 … 0f 00`，密文 `56276ba980b9ec16` → 明文 `01..08`；
- CBC 帧：key=`"12345678ABCDEFGH"`，密文
  `91095162e3f5b6dc6b414b50d1a5b84ec50d0c1b1196fd3c` → 明文 `01..08`；
- 加密侧（自测合成样本用）另与上述向量互证，并做 0..499 长度随机往返测试。

### 3.3 simpleMakeKey

```python
simpleMakeKey(seed=106, n=8):
  out[i] = int(abs(tan(seed + i*0.1)) * 100.0) & 0xff
# 结果（关键常量，代码中断言校验）:
# 69 56 46 38 2B 20 15 0B
```

TEA 密钥 16B = `[s0,h0,s1,h1,...,s7,h7]`（s=simpleMakeKey，h=ekey 解码后前 8 字节）。

### 3.4 QMC2 Map 密码（短密钥 ≤300B）

```
mask(offset):
  o = offset ; if o > 0x7FFF: o %= 0x7FFF
  idx = (o*o + 71214) % N
  return rot8(key[idx], idx & 7)     # rot8(v,i) = 循环左移 (i+4)%8 位
plain[i] = cipher[i] ^ mask(offset+i)
```

### 3.5 QMC2 RC4 变体（长密钥 >300B，本任务实际路径）

参数（与 pushbox/qmc2 C++ 和 ownlight6 Rust 一致）：
- S 盒大小 = 密钥长度 N（N=512 时 `S[i] = i & 0xff`）；
- KSA 同 RC4，模 N；
- `hash = 密钥非零字节连乘（u32 回绕，遇 0 或结果≤当前值即停）`；
- `segkey(id, seed) = int(hash / ((id+1)*seed) * 100.0)`（f64 除法，inf 饱和为 u64::MAX）；
- **首段 0x80 字节**：
  ```
  mask = key[ segkey(offset, key[offset % N]) % N ]
  ```
- **后续按 0x1400 分段**：每段重新复制 S 盒，丢弃
  `(segkey(seg_id, key[seg_id & 0x1ff]) & 0x1ff) + (offset % 0x1400)` 字节后 PRGA；
  seg_id = offset // 0x1400。

验证：`悪魔の子.mgg` 头 8 字节密文 `00 53 15 3b 7a 37 36 71`，
按上述规则 XOR 后 = `4f 67 67 53 00 02 00 00` = `"OggS\x00\x02..."`，且 ffprobe 全曲合法。

### 3.6 QMC1 Static 盒（最老一代）

- 256 字节固定盒（unlock-music `QmcStaticCipher`）：
  `mask(i) = box[(i*i + 27) & 0xff]`，i > 0x7FFF 时 `i %= 0x7FFF`；
- 官方客户端 `libencrypt.so` 的等价优化形式是 64/128 字节 zigzag 表
  （首字节 `C3 4A D6 CA 90 67 F7 52 ...`），`index = (offset % 0x7fff) & 0x7f`，
  高半部分镜像读取，周期 0xFFFE。两种形式均已实现/验证。

### 3.7 ekey 获取接口 GetEVkey

```
POST https://u.y.qq.com/cgi-bin/musicu.fcg
comm: { authst, ct:"19", cv:"1859", uin, tmeLoginType:"3" }
req_1: {
  module: "music.vkey.GetEVkey", method: "CgiGetEVkey",
  param: { filename:[Q{mid}.ext], guid:"10000", songmid:[mid],
           songtype:[1], uin, loginflag:1, platform:"20", ctx:1 }
}
```

响应 `req_1.data.midurlinfo[0]` 关键字段：`result`（0=成功）、`purl`、
`ekey`（704 字符）、`vkey`、`sip[]`、`expiration`（80400s）。
错误码：`104005`（权限/参数错误，注意 filename 必须带扩展名且 songtype=1）、
`104003`（资源不可用）、顶层 `500001`（authst 失效，工具据此自动停批）。
下载 URL = `sip[0] + purl`（purl 含 vkey 签名参数）。

---

## 4. 凭据提取（macOS NSKeyedArchiver）

路径（新版客户端）：`~/Library/Preferences/com.tencent.QQMusicMac.plist`
（旧路径 `~/Library/Containers/…/Data/Library/Preferences/…` 作为回退）。

`AutoLoginUserInfo` 是 **NSKeyedArchiver 二进制 plist**（1421B，`bplist00` 开头）。
解析算法：

```python
inner = plistlib.loads(blob)
objects = inner["$objects"]
# 递归解析 plistlib.UID 引用（dict/list 逐层替换）
# 找到同时含 "strAuthst" 与 "nUserId" 的 UserInfo 字典
```

实测字段：`nUserId=<UIN>`（与 Win 端同一账号）、`strAuthst`(163 字符)、
`loginType=2`、`strOpenId`、`strAccessToken`、`strRefreshToken`、`strRefreshKey`。

安全约定：authst 只在内存使用；写出的 API 请求文件用后即删；工具支持
`--uin/--authst` 手动传入以完全避开 plist。

---

## 5. 歌单 / 我喜欢 / 下载接口逆向

### 5.1 调查路径（按时间序）

1. 公开资料（copws/qq-music-api、jsososo/QQMusicApi、lx-music）覆盖歌单详情与网页版
   接口，但网页接口需要 Cookie + g_tk，与手头 authst 体系不匹配；
2. GitHub API / jsDelivr / Gitee 交叉抓取参考源码（期间 GitHub 原始 unlock-music
   仓库被 DMCA、jsDelivr 偶发 404、raw.githubusercontent 被网络阻断）；
3. **决定性一步**：直接 `strings /Applications/QQMusic.app/Contents/MacOS/QQMusic`
   （88MB 二进制，25.7 万行字符串），找到客户端真实的 module/method 常量：
   `music.musicasset.PlaylistBaseRead.GetPlaylistInfoDiff`、
   `music.musicasset.PlaylistFavRead.GetPlaylistInfoDiff`、
   `music.srfDissInfo.DissInfo.CgiGetDiss`、
   `music.favorSystemRead.FavorSystem.get_favor_list` 等，以及相邻的参数/响应字段名；
4. 用本机 authst 对 musicu.fcg 逐参探测收敛（错误码 500003/860100001 → 参数错误，
   10006 → 缺字段；`v_cache` 从 int 改为 `[]` 后成功）；
5. 分页实测：`song_num=50` 翻页正确，`song_begin` 为起始序号，`hasmore` 终止标志。

### 5.2 接口清单（全部本机实测）

| 用途 | module.method | param | 响应要点 |
|---|---|---|---|
| 创建歌单列表 | `music.musicasset.PlaylistBaseRead.GetPlaylistInfoDiff` | `{uin, v_cache:[]}` | `data.v_playlist[]`（tid/dirId/dirName/songNum） |
| 收藏歌单列表 | `music.musicasset.PlaylistFavRead.GetPlaylistInfoDiff` | 同上 | `data.v_list[]`（tid/name/songnum） |
| 歌单详情 | `music.srfDissInfo.DissInfo.CgiGetDiss` | `{disstid, dirid:0, host_uin:"0", login_uin, song_num:50, song_begin}` | `data.songlist[]`、`total_song_num`、`hasmore`、`dirinfo.title` |
| 加密下载 | `music.vkey.GetEVkey.CgiGetEVkey` | 见 §3.7 | purl + ekey |
| 普通下载 | `vkey.GetVkeyServer.CgiGetVkey` | 同上但 `songtype:[0]` | purl（无 ekey） |

「我喜欢」= 创建歌单列表中 `dirId==201` 的那条（实测 tid=7598475159，307 首），
详情接口对 `dirid` 不敏感（0/201/37 均返回相同 songlist），因此按 tid 即可。

songlist 条目关键字段：`mid`（songmid，即资源 ID）、`name`、
`singer[].name`、`album.name/mid`、`interval`、`songtype`。

### 5.3 下载流水线（每首歌）

```
音质优先级列表（--quality，默认 flac,320,m4a）
  ↓
加密音质（flac/320/192/128）:
  GetEVkey(前缀+mid.ext) → 失败/104xxx → 下一音质
  成功 → GET sip[0]+purl → 若尾部带 musicex footer 则剥离
       → parse_ekey → QMC2 Map/RC4 解密 → sniff 签名（OggS/fLaC/ID3/MPEG/ftyp）
       → 签名合法 → 写文件；不合法 → 下一音质
  ↓
普通音质（m4a/mp3-128/mp3-320）:
  CgiGetVkey → GET purl → sniff → 直接落盘
  ↓
写盘命名: <out-dir>/<歌单名>/<歌手>、<歌手> - <歌名>.<sniffed ext>
ffmpeg: -c copy -metadata title/artist/album 写入标签（可选）
```

## 6. 工具架构

单文件 `qqmusic_decrypt.py`，无第三方运行时依赖（`plistlib/sqlite3/urllib/base64/argparse` 等
全 stdlib；标签写入用系统 `ffmpeg`，缺失自动跳过）。

主要模块：

| 块 | 职责 |
|---|---|
| `tc_tea_*` / `simple_make_key` / `derive_tea_key` | 腾讯 TEA 加解密（含帧校验） |
| `parse_ekey` | EncV2→EncV1→流密钥 |
| `MapCipher` / `RC4Cipher` / `StaticCipher` | 三种流密码 |
| `parse_file` | footer 探测（musicex/QTag/V1/cache/static） |
| `load_macos_credentials` | NSKeyedArchiver 凭据提取 |
| `call_get_evkey` / `call_get_vkey` / `fetch_url_bytes` / `_urlopen` | API/下载层（SSL 证书链缺失自动降级并告警） |
| `SongDb` | 歌曲 sqlite 元数据（缓存文件 API 参数 + 输出命名） |
| `BatchContext` | ekey/vkey 缓存、限速、authst 失效停批 |
| `get_created/collected_playlists` / `get_playlist_detail` | 歌单接口 |
| `download_song_bytes` / `download_playlist_songs` | 在线下载流水线 |
| `process_file` | 本地解密流水线 |
| `InteractiveCli` | 交互式菜单（无参数启动；复用上面全部函数，不复制业务逻辑） |
| `run_self_test` | 7 组离线自测 |

关键设计：
- 无参数启动进入交互式菜单（`-i/--interactive` 强制），带参数保持批处理/脚本化不变；
- 交互层直接复用批处理函数（歌单下载、`process_file`、设置变更即改 `opts` 生效）；
- 同 (filename, songmid) 的 ekey/vkey 在进程内缓存，避免重复 API；
- API 成功后默认 0.15s 间隔；`500001/authst` 错误置 `ctx.stop` 停批；
- 本地重跑先按后缀猜输出路径（mgg→ogg、mflac→flac 等），已存在则 SKIP，**0 次 API**；
- 签名嗅探决定真实扩展名（不盲信源后缀）；
- 歌单分页 `song_num=50` 循环至 `hasmore=0`，`--limit` 每歌单截断；
- `--dry-run` 只列歌曲不调 vkey；`--info` 只解析 footer。

---

## 7. 验证与证据

### 7.1 离线自测（`--self-test`，全绿）

1. TC-TEA ECB/CBC 官方向量（jixunmoe/tc_tea_rust）；
2. simpleMakeKey 期望字节 `69 56 46 38 2B 20 15 0B`；
3. EncV1/EncV2 包装往返（随机 512B 密钥）；
4. Map（128B key）/ RC4（308B key）加解密往返；
5. QTag / V1 合成文件生成→解析→解密往返；
6. QMC1 Static 往返；
7. musicex footer 解析（含实测字段值）。

### 7.2 真实文件解密证据

| 输入 | 路径/模式 | 输出 | 证据 |
|---|---|---|---|
| 悪魔の子.mgg | Downloads（musicex） | `悪魔の子.ogg` 2,653,740B | `OggS` 头、ffprobe 227.7s、623 Ogg 页 |
| 391934297-13.mgg | iMusic 缓存 | `赵寒 - Do I Matter To Me.ogg` 7,727,890B | 203.5s，O8M0 前缀命中 |
| 551010366-13.mflac | iMusic 缓存 | `三Z-STUDIO_HOYO-MiX - 野火.flac` 24,205,765B | 114.1s，F0M0 命中 |
| 551010366-13.mgg | iMusic 缓存 | `… - 野火.ogg` 4,718,625B | 114.1s |
| 105030812-13.mflac | iMusic 缓存 | `Alan Walker - Faded.flac` 27,001,869B | 212.6s |
| 我喜欢 #1 | 在线下载 | `Come Alive Stripped.flac` 50,056,609B | 215.3s，标签 title/artist/album 正确 |
| 歌单 9028251943 #1 | 在线下载 | `Zerky、Liu - Temple.ogg` 11,491,456B | 295.2s |
| 我喜欢 #1（m4a 兜底） | 在线下载 | `Come Alive Stripped.m4a` 2,603,550B | 215.3s |

### 7.3 交叉验证

- footer song_id `0x141d30ce` ↔ DB id `337457358` ↔ 歌曲名/歌手；
- footer media_mid ↔ DB `K_SONG_RESERVE1/K_SONG_RESERVE8`；
- DB filesize ↔ 密文长度（逐字节）；
- 缓存下载 FLAC（50,056,609B）与本地 iMusic 同曲缓存（551010803-13.mflac，50,056,609B）
  **字节数一致**，证明下载+解密链与客户端自身产物等价。

---

## 8. 遇到的问题与修复（全部留痕）

| # | 问题 | 定位 | 修复 |
|---|---|---|---|
| 1 | Python 逐字节双重循环扫描 300s 超时 | 2.6MB × 11 模式纯 Python 嵌套 | 改用 C 级 `bytes.find` 循环 + 分块熵统计 |
| 2 | 含日文文件名的 heredoc 导致 PTY 中断 | 工具层 UTF-8 传输 | 脚本全 ASCII，Python 内 `glob` 解析路径 |
| 3 | 首次解密输出未变 | `data[:audio_len]` 切片产生副本，密文没被改 | 直接对切片对象原地解密 |
| 4 | 首版 RC4 分段 skip 循环 `prga(s,j,k)` 不更新 j/k（int 传值） | 分段后全是噪音 | 循环内联 j/k 更新 |
| 5 | TC-TEA 加密自测失败 | `enc_round` 未回写 iv1/iv2（调用了但丢弃返回值） | nonlocal 回写 |
| 6 | EncV2 自测失败 | 包装顺序写反：应先 encrypt(K2) 再 encrypt(K1) | 与 ownlight6 解密顺序对齐 |
| 7 | Python SSL 证书链缺失（CERTIFICATE_VERIFY_FAILED） | 系统 Python 无 CA | `_urlopen` 自动降级 unverified + 一次性告警 |
| 8 | iMusic 缓存解不出 | 前缀猜 O4M0/M800 错 | 全前缀矩阵实测 → O8M0 命中；M8M0 属无权限 |
| 9 | 歌单接口 500003/10006 | `v_cache` 传 int / 模块名旧 | 客户端二进制 strings 找到真 module.method；`v_cache:[]` 成功 |
| 10 | ffmpeg 标签未生效 | 临时文件 `.tag.tmp` 让 ffmpeg 无法推断 muxer | 临时名保留原扩展名 `.tag.flac` |
| 11 | 重跑本地解密浪费 API | 先解密才知道输出名 | 后缀→扩展名预判，已存在直接 SKIP（API=0） |
| 12 | xlog 解码失败 | QQ 音乐使用私有 ECDH 密钥加密日志（非 Mars 默认密钥） | 放弃该路线，改二进制 strings + 实测；临时 dec.log 已清理 |
| 13 | 第三方文档两处错误 | musicex 尾部偏移表；`#!Qk` 魔数说法 | 以本机 hexdump 与实现代码为准，文档已注明 |

---

## 9. 安全、合规与数据清理

- authst/ekey/vkey 均为内存变量；临时 JSON（含凭据）用后 `rm` 删除；
- 未对 QQ 音乐客户端做任何修改、注入或二进制补丁（只读 strings 与 API 调用）；
- API 调用与客户端正常行为等价（同 comm/authst、platform=20、UA/Referer 标准值）；
- 工具内置 `--delay` 限速、`--dry-run`、authst 失效即停，避免误刷接口；
- 只处理你本人账号资源；不提供分享/传播能力。

## 10. 已知限制与 BACKLOG

- 单线程；全量「我喜欢」307 首预计 15~30 分钟（受限速与 CDN 影响）；
- M8M0（320k MP3 加密资源）本账号无权限（104003），自动跳过；
- 歌单接口参数为 2026-08 客户端版本实测，未来可能随版本变化；
- BACKLOG：Windows authst 自动提取（内存扫描）、并发下载、歌词/封面嵌入、
  `.tkm` M4A 头修复、O6M0/O4M0 名义码率实测确认。

## 11. Windows 端一致性研究（2026-08-18 SSH 实测）

结论先行：**密文/ekey/TEA/QMC2 密码/质量前缀完全一致；差异集中在 3 处**——
① 客户端 API 的 module/method 命名，② 凭据存储方式（无持久化 authst），
③ 缓存布局与 musicex footer 的有无。

### 11.1 环境

- 机器：`PC-*`（Win11 build 26200，SSH/FRP 登录 `<WIN_USER>`）
- 客户端：QQ 音乐 **21.21**，`<QQMUSIC_INSTALL>\QQMusic.exe`（运行中，主逻辑在
  `QQMusic.dll` 12MB / `QQMusic_Protocol.dll` 1.9MB）
- 账号：`%APPDATA%\Tencent\QQMusic\QQMusicServiceConfig.ini`
  `[Account] Uin=<UIN>` —— 与 Mac musicid **完全相同**
- 缓存根：注册表 `HKCU\Software\Tencent\QQMusic` → `CACHEPATH=<QQMUSIC_CACHE>`

### 11.2 缓存格式实测对比

Win 缓存布局（与 Mac 不同）：

```
<QQMUSIC_CACHE>\downloadproxyNew\tp2p\.tpfs\duty\
  <前缀+mid>.<ext>\            ← 目录（如 O4M0001WZk3f29SpSn.mgg\）
    <前缀+mid>.<ext>           ← 原始密文（无任何 footer）
    .property                  ← 167B 下载元数据（含文件名、哈希、form-urlencoded 头）
    tp                         ← ~3.7KB 二进制（暂未解出，疑似 ekey/任务元数据）
```

关键证据：scp 取回 `O4M0001WZk3f29SpSn.mgg`（2,653,740B），与 Mac
`悪魔の子.mgg` 的前 2,653,740 字节**逐字节相等**（sha 比对通过）；
Mac 文件 = Win 缓存 + 末尾追加 192B musicex footer。即：
**Win 客户端不写 musicex footer，Mac 下载时追加**。缓存目录内另有 234 个
`F0M0*.mflac`，前缀方案与 Mac 一致。

### 11.3 端到端解密一致性（platform=27 实测）

1. authst 仅存在于 `QQMusic.exe` 进程内存（配置文件中只有 Uin）。用 ctypes
   （OpenProcess/VirtualQueryEx/ReadProcessMemory，含 Win10 1803+ 的
   PartitionId 字段）扫描进程内存 `"authst":"…"`，取到多个 160 字符候选，
   其中 1 个有效（临时脚本执行后已删除）；
2. 用该 authst + `platform:"27"` 调 `music.vkey.GetEVkey.CgiGetEVkey`
   （filename=`O4M0001WZk3f29SpSn.mgg`）→ `result=0`，704 字符 ekey；
3. `parse_ekey` → 512B 流密钥 → QMC2 RC4 解密 Win 缓存文件 →
   `OggS` 头 + `Ogg data, Vorbis audio, stereo, 44100 Hz`，
   ffprobe duration = **227.679887s**，与 Mac 解密结果完全一致。

### 11.4 API module/method 差异表（本机实测）

| 功能 | Mac 客户端 | Win 客户端 | 参数差异 |
|---|---|---|---|
| 创建歌单列表 | `music.musicasset.PlaylistBaseRead.GetPlaylistInfoDiff` | `music.musicasset.PlaylistBaseRead.GetPlaylistByUin` | 均 `{uin, v_cache:[]}`；返回 v_playlist **10 条逐项相同** |
| 收藏歌单列表 | `music.musicasset.PlaylistFavRead.GetPlaylistInfoDiff` | `music.musicasset.PlaylistFavRead.GetPlaylistFavInfo` | 均 `{uin, v_cache:[]}`；返回 v_list **4 条逐项相同**；Win 缺 uin → 80050 |
| 歌单详情 | `music.srfDissInfo.DissInfo.CgiGetDiss` | `music.srfDissInfo.DissInfoForPc.uniform_get_Dissinfo` | Win 只收 `{disstid, song_num, song_begin}`；带 `dirid/host_uin/login_uin` → **code 10006**；返回 songlist/dirinfo 同构 |
| 我喜欢列表 | 同歌单详情（dirId=201） | 同（tid 同值 7598475159，307 首） | — |
| 加密/普通下载 | `music.vkey.GetEVkey` / `vkey.GetVkeyServer` | 同 | `platform` 参数 **20 → 27** |
| 收藏同步 | `music.favorSystemRead.FavorSystem.get_favor_list` | `music.favor_system_read.get_favor_list`（下划线命名） | 未深入使用 |

Win 另有 `music.musicasset.PlaylistDetailRead.GetUniformSongDetailInfo`
（参数含 `enc_uin`、`bPaged`）作为歌单详情的备选接口。

### 11.5 工具适配（已完成并在 Win 端实测）

脚本现为单文件双平台自动分支：

- `IS_WINDOWS` 检测 → `API_PLATFORM` 自动 27/20（`--platform` 可强制）；
- Win 凭据：`_win_read_uin`（ini）+ `_win_memory_authst_candidates`
  （ctypes 内存扫描，含 PartitionId 字段）→ `load_windows_credentials`；
  内存中可能残留多个 authst，`BatchContext._api_with_creds` 失败时自动轮换候选；
- Win 缓存：`win_cache_root()`（注册表 `CACHEPATH`）→ duty 目录自动发现；
  `parse_file` 从 `<前缀+mid>.<ext>` 文件名直接识别 `win_cache`（无 footer、无需 DB）；
- Win 接口：`get_created/collected_playlists`、`get_playlist_detail` 按
  `API_PLATFORM` 切换 §11.4 表中的 module/method；
- Win 控制台输出统一 UTF-8（reconfigure + errors=replace，emoji 歌单名不再崩）；
- sqlite3/plistlib 按平台惰性降级（embeddable Python 可运行）。

Win 端实测（`<PYTHON>` 3.12.9 + QQ音乐 21.21）：
`--self-test` 全绿；duty 缓存 `O4M0001WZk3f29SpSn.mgg` 自动解密为
`…\Music\QQMusicDecrypted\O4M0001WZk3f29SpSn.ogg`（2,653,740B，227.7s）；
`--list-playlists` 与 Mac 结果一致；`--favorites --limit 1 --quality flac`
在线下载解密成功（50,056,609B，215.3s）。

## 12. 参考来源

- [unlock-music 源码（Gitee 镜像）](https://gitee.com/ix64/unlock-music)：`qmc.ts/qmc_cipher.ts/qmc_key.ts/tea.ts`
- [jixunmoe/tc_tea_rust](https://github.com/jixunmoe/tc_tea_rust)：腾讯 TEA 权威实现与测试向量
- [TarsCloud/TarsCpp tc_tea.cpp](https://github.com/TarsCloud/TarsCpp)：C++ 原生 TEA（16 轮 CBC 帧）
- [pushbox/qmc2](https://github.com/pushbox/qmc2)：QMC2 `KeyDec.cpp/StreamCencrypt.cpp`（C++ 参考）
- [ownlight6/qmc-decoder](https://github.com/ownlight6/qmc-decoder)：musicex 格式、GetEVkey 流程（Rust）
- [ix64 MGG/MFLAC 研究 gist](https://gist.github.com/ix64/bcd72c151f21e1b050c9cc52d6ff27d5)：2021 密钥格式、官方 Static 实现、URL 前缀反编译
- [nullptr-0/QmcWasm](https://github.com/nullptr-0/QmcWasm)、[nullptr-0/QmcDll](https://github.com/nullptr-0/QmcDll)：legacy 实现
- [copws/qq-music-api](https://github.com/copws/qq-music-api)、[jsososo/QQMusicApi](https://github.com/jsososo/QQMusicApi)：歌单/播放接口旁证
- 本机 QQ 音乐 Mac 客户端二进制（`/Applications/QQMusic.app/Contents/MacOS/QQMusic`）：module/method 常量来源
- 远程 Win 客户端二进制 `<QQMUSIC_INSTALL>\QQMusic_Protocol.dll`（v21.21）：Win 侧 module/method 常量来源
- [GitHub DMCA 2022-11-04-qqmusic](https://github.com/github/dmca/blob/master/2022/11/2022-11-04-qqmusic.md)
