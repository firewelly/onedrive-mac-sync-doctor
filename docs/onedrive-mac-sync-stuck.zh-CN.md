# OneDrive for Mac 卡在「正在同步 N 个文件」到底在卡什么及解决办法

> 2026-10-30 实测记录。两个账户，一个卡在 307、一个卡在 1。中间我一度误判成「客户端版本 bug」，白折腾了一整晚的降级与回退，最后才发现真凶是元数据里一批指向已删除路径的幽灵记录。
> 本文的数字、日志片段、SQL 都是真机实测，不是通用建议的复述。

---

## TL;DR

macOS 上 OneDrive 一直显示「正在同步 N 个文件」却永远不动，**九成不是网络问题、不是登录问题、也不是客户端版本 bug**，而是 `dbfs.db` 里有一批记录指向磁盘上已经不存在的路径。同步引擎每次重扫都在解析它、每次都失败。

判断入口只有一个：

```sql
-- ~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_<账户>.noindex/dbfs.db
SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned = 0 AND ph_insync = 0;
```

**这个数字约等于界面上的 N**（实测一个账户 346 → 界面 307，另一个 9 → 界面 1）。

把这批条目的路径逐条解析出来、看哪些不存在，然后分五类处理：

| 类型 | 处理 |
|---|---|
| 幽灵记录（路径已不存在） | tombstone 元数据记录，**不要**动文件 |
| 符号链接 / AppleDouble | 直接删掉，它们本来就不该在 OneDrive 里 |
| 解压残留（`__MACOSX/`） | 整目录删 |
| 构建产物 / 锁文件 | 删，重新构建会再生 |
| 顽固单文件（文件在、但传不动） | 重建文件 |

官方那套「重置 / 注销域」能用，但**三个月就会复发**，而且会清掉本地未上传的副本。下面给的是更轻、更精准、可反复执行的路线。

---

## 一、先理解它的数据放在哪

不要一上手就「重置」。OneDrive for Mac 的本地状态分三层，故障也分层。

### 1. 元数据数据库（真正的故障现场）

```
~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/
├── .Dbfs.Dbfs_<账户>.noindex/dbfs.db     # 每个账户一个，1 GB 上下
└── FileProviderLogs/                      # 扩展日志，故障时会爆量
```

`dbfs.db` 是 SQLite（schema `version=7`），核心两张表：

**`dbfs_mftrecord`** —— 「我认为文件系统里有什么」。百万行级别。关键列：

| 列 | 含义 |
|---|---|
| `itemid` / `parentid` | 自引用树，根记录是 `itemid=1, parentid=1` |
| `name` | 名字，可能带内部前缀 `//v0/<34>//` |
| `tombstoned` | 0=存在，1=已删除 |
| `materialized` | 1=内容在本地，0=占位符（内容在云端） |
| **`ph_insync`** | **0=未同步——界面那个 N 就是它的计数** |
| `content_hash` / `size` | 与云端比对用 |

**`dbfs_changerecord`** —— 「还有哪些本地改动等着上传」。关键列 `itemid`、`change_type`。

两者的关系是整个问题的核心：

> **变更记录说「这个文件要上传」，元数据说「这个文件在哪」。一旦元数据描述的路径在磁盘上不存在，这条变更就永远处理不完。**

### 2. File Provider 域（系统侧）

```bash
fileproviderctl dump -l      # 查看域状态
ls ~/Library/CloudStorage/    # 挂载点
```

域由 macOS 管理，并记录「是哪个版本的 OneDrive 创建/升级了它」。这一条后面会变成一个大坑。

### 3. 同步引擎日志

```
~/Library/Logs/OneDrive/<账户>/SyncEngine-*.odl     # 二进制，用 strings 读
```

故障时的死循环：

```
ProcessLocalChange LC_REPLACE_FILE 3
UnexpectedFailure!
PostponedChanges.cpp  SetPostponedStatus
PostponedChanges.cpp  PostponeChange
Diagnostics.cpp       RunDiagnostics
Diagnostics.cpp       RetryPostponedChanges     ← 无限重复
```

再往上翻几行才是真正的错误源：

```
DbfsMacboxGetUserVisibleItemPath
Errno::2 (0x2) [NotFound] [source:0x049d02d2]
...
OdfsDbfs::-615838174 (0xdb4b0e22) [Permanent]
FileForRead.cpp  Read
ReplaceFile.cpp  CompareDiskFileAgainstDB
```

`DbfsMacboxGetUserVisibleItemPath → NotFound` 的含义是：**数据库里的 itemid 映射不回磁盘路径。**

---

## 二、关键判据：界面那个 N 到底是什么

这是全文最重要的一句话：

> **界面的「正在同步 N 个文件」≈ `SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0 AND ph_insync=0`**

实测对照：

| 账户 | `ph_insync=0` | 界面显示 |
|---|---|---|
| A | 346 | 307 |
| B | 9 | 1 |

不完全相等（界面只统计它认为「真有活要干」的子集），但量级和趋势一致，足够用来定位与验收。

**顺带一个反直觉的结论**：`dbfs_changerecord` 的总行数**不能**当进度看。故障时队列是 25,020 行，但界面上只有 307——另外两万多条是已经和云端对齐、只差元数据收尾的记录。我一开始盯着队列行数等了很久，完全白等。

---

## 三、排查流程

### 步骤 1：拿到当前数字

```bash
DB=~/Library/Group\ Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_Personal.noindex/dbfs.db
sqlite3 "$DB" "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0 AND ph_insync=0;"
```

### 步骤 2：把这批条目解析成真实路径

`name` 字段带内部前缀 `//v0/<34>//` 要去掉；路径按 `parentid` 逐级拼。输出形如：

```
id=834948  type=1  exists=1  Documents/skills/foo/SKILL.md
id=1070358 type=3  exists=0  <ghost>/python
```

`type`：1=文件，2=目录，3=符号链接。

### 步骤 3：按 `exists` 分堆

```
exists=0            → 幽灵记录，清元数据
type=3              → 符号链接，删
type=1 且 exists=1  → 真实文件，进一步判断
```

### 步骤 4：看目录级规模（比逐文件快得多）

如果一整棵子树都不存在，别逐个处理，直接整树 tombstone：

```sql
SELECT m.parentid, p.name, COUNT(*) AS n
FROM dbfs_mftrecord m
LEFT JOIN dbfs_mftrecord p ON p.itemid = m.parentid
WHERE m.tombstoned = 0 AND m.ph_insync = 0 AND m.itemtype = 1
GROUP BY m.parentid
ORDER BY n DESC;
```

实测输出一眼就能定位：

```
1293203|//v0/<34>//_trash|305     ← 305 条全在这一个目录下
1548306|//v0/<34>//some-skill|7
...
```

### 步骤 5：确认这个目录真的不在磁盘上

```bash
ls -ld "$MOUNT/Documents/Archive/_trash"
# ls: No such file or directory
```

而数据库里它还有 **7,136 条**子记录、全部 `tombstoned=0`。

**这就是故障本体**：几个月前清空的回收站目录，磁盘上早没了，数据库里还挂着，引擎每次重扫都要解析它。

### 步骤 6：排除「内容没下载」的干扰

```sql
SELECT materialized, COUNT(*), SUM(size)
FROM dbfs_mftrecord
WHERE tombstoned = 0 AND itemtype = 1
GROUP BY materialized;
```

`materialized=0` 是去水化占位符（内容在云端），读它会触发下载；`materialized=1` 才是内容在本地。

对 `materialized=1` 的文件做完整读取测试是安全的：

```bash
shasum -a 256 "/path/to/file"     # 秒返回 = 内容确实在本地
```

---

## 四、五类卡住的东西

### 类型一：幽灵记录（数量最多，本次占 88%）

路径在磁盘上已经不存在，数据库里还标着存在。

本次 305 条全指向同一个 `_trash` 目录：磁盘早清空，数据库里仍挂着 **7,136 条**记录、`tombstoned=0`。

根源通常是：某个时刻删了/改名了目录，但当时同步没在正常工作，删除没有被完整记录，旧记录成了孤儿。

### 类型二：符号链接

OneDrive 不处理符号链接，但链接会进数据库，然后永远同步不上去。

典型来源：

- Flutter 的 `macos/Flutter/ephemeral/`、`linux/flutter/ephemeral/`——全是插件链接，`flutter pub get` 每次重建
- Python 的 `venv/`、`.conda/` 里的 `bin/python`、`bin/python3`
- `node_modules/.bin/`

### 类型三：解压残留

`__MACOSX/` 目录。解压 macOS 打包的 zip 会产生，里面**只有** `._*` AppleDouble 文件，OneDrive 永不同步。

实测某个技能目录下有 25 个这样的文件，共 100 KB，纯垃圾。

### 类型四：构建产物与锁文件

`.dart_tool/`、`target/`、`dist/`、各种 `.lock`。

特点是**每次构建都重建**，所以每次都在库里留下新记录、让旧记录失效。数量不多但持续产生噪声。

### 类型五：顽固单文件

文件确实在磁盘上、能完整读取、`shasum` 秒过，但那条「替换文件」（`LC_REPLACE_FILE`）记录就是处理不完。

特征很一致：`materialized=1`、带着一条 `change_type=4`、`ph_insync=0`。数量很少（本次 2 个）。

有意思的是，日志里那种 `Errno::2 NotFound` 看起来像「文件没了」，实际上这批文件**全都在**。所以千万别据此判断数据丢失——直接 `shasum` 一下就知道。

---

## 五、解决方案

按代价从低到高，**能用第一档就不要用第四档**。

### 第 1 档：只删变更记录（零云端副作用）

适用：路径已不存在、但有大量 `changerecord` 在排队重试。

```sql
-- 先备份！退出 OneDrive 并确认 lsof 无占用
DELETE FROM dbfs_changerecord WHERE itemid IN (
  WITH RECURSIVE p(itemid) AS (
    SELECT <幽灵子树根 itemid>
    UNION
    SELECT r.itemid FROM dbfs_mftrecord r JOIN p ON r.parentid = p.itemid
  ) SELECT itemid FROM p
);
```

**只动 `changerecord`，不动 `mftrecord`，所以不会向云端发起任何删除。**

实测：某账户一棵改过名的旧目录树下有 2,419 个文件各排队约 32 次，共 **76,797 条 = 队列的 75.7%**。删掉后 `change_type=4` 从 76,770 降到 83。

但要注意：**这一档只能清掉「重试洪水」，不能解决「界面卡住」**——卡住的那批条目根本没有 `changerecord`。见第 3 档。

### 第 2 档：删掉不该进 OneDrive 的东西

不需要任何数据库操作，删文件即可：

| 目标 | 原因 |
|---|---|
| `__MACOSX/` | 解压残留，只有 `._*` 垃圾 |
| `macos|linux/flutter/ephemeral` | 插件软链农场，重新构建会再生 |
| `.dart_tool/` | 构建缓存，含锁文件 |
| `venv/`、`.conda/` | Python 的 `bin/python*` 是软链 |
| `node_modules/` | 体积大且常含软链 |

### 第 3 档（关键）：tombstone 幽灵目录树

这才是解决「界面卡在 N」的正招。

数据库有触发器卡着顺序：

```sql
CREATE TRIGGER enforce_constraint_update_tombstone
BEFORE UPDATE OF tombstoned ON dbfs_mftrecord FOR EACH ROW
WHEN (NEW.tombstoned = 1) BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM dbfs_mftrecord WHERE parentid = NEW.itemid AND tombstoned = 0
  ) THEN RAISE(ABORT, 'record has at least one non-tombstoned child') END;
END;
```

所以必须**按深度倒序**，子级先于父级。实测结果：

```
子树记录数: 7136   其中未删除: 7136
已 tombstone 7136 条
子树内剩余未删除: 0
integrity: ok
```

`ph_insync=0` 立刻从 **346 → 41**。

### 第 3.5 档：清完剩下的零散条目

剩下的 41 条拆开看：9 条失效的插件软链、25 个 `__MACOSX/._*`、4 个 `.lock`、3 条路径已消失的文件。删掉文件后全部 `exists=0`，统一 tombstone：

```
已 tombstone: 40（跳过系统 temp 记录）
剩余未同步: 1
```

**346 → 1**，那个 1 是系统记录（见坑 1）。

### 第 4 档：重置域（不推荐）

官方支持的「Unlink / Reset」，以及 App 自带的：

```
/Applications/OneDrive.app/Contents/Resources/ResetOneDriveAppStandalone.command
```

实际做的事：

1. `rm -rf ~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite` —— **各账户的 `dbfs.db` 加上数十 GB 内容缓存全没**
2. `rm -rf ~/Library/Application Support/OneDrive`、`~/Library/Logs/OneDrive`
3. `OneDrive /removedomains` —— 注销 File Provider 域，`~/Library/CloudStorage/` 下的挂载点消失
4. 删掉钥匙串里的登录凭据

对干净账户没问题。但如果队列里有**从未上传成功的本地副本**（本次一台机器就有 2.4 万个文件、1.5 GB），这一套等于直接丢数据。

而且效果不持久：**大约三个月复发**，因为导致问题的根源一个都没解决。

---

## 六、四个真实的坑

### 坑 1：系统 `temp` 记录永远无法 tombstone

`itemid = 9223372036854775708` 的 `temp` 记录，`parentid` 指向自己：

```
id=9223372036854775708 type=2 name=temp
    child: 9223372036854775708 temp tombstoned=0    ← 它自己
```

触发器看到「有未删子项」就永远 ABORT。这条记录常驻 `ph_insync=0`，**所以修完之后那个 1 是清不掉的，别以为没修干净**。

### 坑 2：tombstone 之后队列会先暴涨

清完 7,136 条后，`dbfs_changerecord` 从 25,057 涨到 30,467，其中新增 7,156 条 `change_type=260`，全部指向刚被 tombstone 的记录，扩展 CPU 短时回到 95%。

这是**与云端对账**的正常过程，不是恶化。判断标准看 `ph_insync=0` 有没有降、界面 N 有没有回落，而不是队列行数。

### 坑 3：降级是单向门

如果怀疑是客户端版本问题，先读这一段。

`OneDrive.pkg` 的 `Scripts/preinstall` 有降级守卫：

```bash
if [ ${should_continue_preinstall} != 0 ]; then
    od_logging::log_info "A higher version of OneDrive is already installed on this machine. macOS Installer will skip file copy."
    exit 0
fi
```

`exit 0` 意味着**报成功、一个文件都不拷**。要降级必须先 `mv` 走 `/Applications/OneDrive.app`。

但更麻烦的是：旧版装上也**用不了**。File Provider 域被新版升级后会打版本戳，旧版启动直接弹模态框：

> **需要 OneDrive 更新**
> 你的 OneDrive 文件由较新版本的 OneDrive 管理。请更新到最新版本以访问你的文件。未丢失任何数据。

只有一个「退出 OneDrive」按钮，关不掉。此时挂载点枚举还是残缺的（实测某目录数据库里 100 项、磁盘上只看到 79 项）。

最后还有一层：新版自带的更新器会自己把新版拉回来，所以降级是个跑步机。

**结论：不要靠降级解决这类问题。**

### 坑 4：`Errno::2 NotFound` 不代表文件丢了

日志里的 `DbfsMacboxGetUserVisibleItemPath → Errno::2 NotFound` 很容易让人以为文件被删了。实际上本次失败的 24,777 个文件**全部在磁盘上且可完整读取**（`shasum` 秒过）。

这个 NotFound 是 macbox 层（数据库 itemid → 可见路径）的映射失败，不是文件系统的 ENOENT。判断文件在不在，直接 `shasum` 或 `head -c 1`，别信日志。

---

## 七、为什么会反复发作

因为这套体系里没有任何机制阻止你把「不适合同步的东西」放进 OneDrive：

- 符号链接：OneDrive 不处理，但会留记录
- 构建产物：每次重建都在库里留下孤儿
- 删除/改名目录：云端同步了，本地元数据没清干净，几个月后重扫时才爆出来
- 多设备大量同步：冲突副本本身就是额外的元数据噪声

所以它是**周期性维护事项**，不是一次性修复。

---

## 八、预防与体检

1. **构建/环境目录永不放进 OneDrive**：`node_modules`、`venv`、`.conda`、`.dart_tool`、`ephemeral`、`target`、`dist`。
2. **删目录要在 OneDrive 正常运行时删**，删完等它确认同步完成再关机。最怕「删了/改名了，但同步当时是坏的」。
3. **定期体检**（1 分钟）：

```bash
DB=~/Library/Group\ Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_Personal.noindex/dbfs.db
sqlite3 "$DB" "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0 AND ph_insync=0;"
```

个位数 = 健康，涨到几十以上 = 该查了。

4. **动数据库前一定备份**，且必须在 OneDrive 完全退出后操作：

```bash
lsof "$DB"                                    # 应该没有输出
cp -p "$DB" "$DB-wal" "$DB-shm" ~/od-db-backup-$(date +%Y%m%d)/
```

5. **改名/移动大目录后**多等几分钟，看 `ph_insync=0` 有没有回落。没回落就说明留下了幽灵。

---

## 九、验收

修完不该只看「界面不转了」，要同时满足：

- `ph_insync=0` 回到个位数（那个 1 是系统记录）
- `PRAGMA integrity_check` 返回 `ok`
- 新建一个测试文件，能在几十秒内完成上传
- 扩展进程 PID 稳定，不再每 30–60 秒换一批
- CPU 从 80%+ 回落到个位数

本次修复后：`ph_insync` 346 → 1，`integrity_check = ok`，主进程 CPU 从 129% 降到 10%，扩展从 95% 回落。

---

## 十、工具

上面这些 SQL、路径解析、深度排序、事务回滚，整理成了一组脚本：

**github.com/firewelly/onedrive-mac-sync-doctor**

- 全部只读优先，写操作默认 dry-run
- 动数据库前强制检查 OneDrive 已退出
- 事务内执行，删除行数与预期不符则回滚
- 无硬编码路径，账户与挂载点从 OneDrive 自己的配置推导

---

## 写在最后

如果只记一句话：

> OneDrive 卡在「同步 N 个文件」时，先查 `ph_insync=0` 的条目路径是否存在，而不是查网络、查版本、或者重置。

重置能治标，但三个月后你会再写一遍这篇文章。
