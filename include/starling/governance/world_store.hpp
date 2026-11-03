#pragma once

// world_store.hpp — 世界注册表（W1）与快照（W2）的唯一权威实现。
//
// 设计稿：docs/superpowers/specs/2026-10-25-world-scene-management-design.md
//   §3 世界注册表、§4 快照。
//
// 一个世界 = 一个 SQLite 文件 + 一个租户 id。注册表 registry.db（RegistryDb，见 registry_db.hpp）
// 是世界路径的唯一来源：任何 API 都不接受客户端传入文件路径。绑定层（Python / HTTP）只做
// 转发；注册、校验、保留策略与快照语义全部在这里。旧注册表 worlds.json 只剩一次性导入
// （import_worlds_json，registry_db.hpp）。
//
// 并发：调用方必须按世界串行化快照、回滚、修剪、删快照与自动快照（即原来 WorldManager 的管理锁，
// 现在按世界持有）。RegistryDb 自己的互斥锁只保护注册表行：take_snapshot 先读快照列表、再写文件、
// 最后登记，两步之间不持它的锁，同一世界上并发的两次快照会互相把对方刚改好名、还没登记的文件
// 当成残留清扫掉。不同世界之间互不影响。

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace starling::governance {

class WorldStoreError : public std::runtime_error {
public:
    WorldStoreError(std::string code, const std::string& detail);
    [[nodiscard]] const std::string& code() const noexcept;
private:
    std::string code_;
};
// code 取值：invalid_world_name | world_exists | world_not_found | world_protected
//           | registry_corrupt | invalid_retention | snapshot_not_found
//           | insufficient_disk | snapshot_failed
//           | labels_immutable（RegistryDb::set_labels：世界已有标签，标签创建后不可改）
//           | invalid_snapshot_kind（take_snapshot：kind 不是 manual / auto，或 kind 与 reason 不配对）
//           | world_tenant_mismatch（verify_world_tenant：库里有别的租户的行）
//           | snapshot_unsupported_backend（take_snapshot / rollback_world：vector_backend 不是 sqlite）
//           | snapshot_corrupt（rollback_world：快照文件缺失、打不开或 integrity_check 不过）
//           | world_in_use（rollback_world：第 8 步的探测发现别的连接开着要被替换的库。WAL 库上
//             读过库的闲置连接与开着的读事务都看得见；delete 模式的库（恢复出来、引擎还没打开
//             过）上只看得见开着的读事务；只打开、没读过的连接两种模式都看不见。探测只是纵深
//             防御，见 rollback_world）
//           | invalid_auto_snapshot（W4：interval_hours 越界、session 触发缺 session_id、
//             now_iso 解析不了（AutoTrigger 与 SnapshotOptions 的都算）、触发类型非法、
//             list_sessions 的 limit 越界）
//           | invalid_settings（world_settings：设置 JSON 解析失败、未知键、类型错误）
//           | endpoint_changed（require_endpoint_unchanged：继承来的 provider 的 base_url 已被改动）

struct SnapshotEntry {
    std::string id;           // 20261025T011200Z-a1b2
    std::string path;         // 绝对路径
    std::string kind;         // "auto" | "manual"
    std::string reason;       // manual → "manual"；auto → "session" | "interval"（注册表校验）
    std::string note;
    std::string created_at;   // ISO-8601 UTC 秒精度
    std::string world_time;   // 快照覆盖到的世界时刻水位，可为空
    std::int64_t size_bytes = 0;
    // 以下三项 W4 新增，读取都是可选的（W2/W3 的条目没有）。
    // 快照时该世界已有的会话数（source_unit_meta 的 DISTINCT session_id）。缺省时 JSON 不写。
    std::optional<std::int64_t> sessions_complete;
    // 触发它的会话（只有 reason="session" 时非空）。为空时 JSON 不写。
    std::string session_id;
    // 内容标记（格式见 take_snapshot 第 2a 步）。为空时 JSON 不写；空串也表示「库里没有内容」。
    std::string content_mark;
};

// 每个世界的自动快照配置（W4）。存在世界设置 settings_json 的 background.auto_snapshot 里
// （world_settings.hpp），JSON：{"on_session": bool, "interval_hours": int}。
//   on_session：某个 session_id 在该世界第一次写入之前先拍一份；
//   interval_hours：距上一份自动快照超过这么多小时、且有新写入时拍一份；0 表示关闭，上限 8760。
struct AutoSnapshotConfig {
    bool on_session = false;
    std::int64_t interval_hours = 0;
    bool operator==(const AutoSnapshotConfig&) const = default;
};

[[nodiscard]] bool is_valid_world_name(std::string_view name);

class RegistryDb;   // registry_db.hpp：世界注册表 registry.db（SQLite）

// ---- 注册表不变式（取代 worlds.json 时代 load_registry 的校验） -----------------
//
// 注册表行只由 C++ 写（RegistryDb 的方法、import_worlds_json），路径都由 dir_id 推出；这里查的是
// 手改过的 registry.db 与被换成符号链接的世界目录。下面的快照、回滚、修剪、自动快照函数本身不跑
// 这些检查：以前 WorldManager 每次操作之前都 load_registry，整份注册表校验一遍；现在由调用方在
// 启动时调用 validate_registry，在打开世界时、以及每次快照 / 回滚 / 删快照之前调用 validate_world。
//
// validate_world 只查一个世界（世界不存在抛 world_not_found；保留份数 < 2 抛 invalid_retention），
// 其余违反都是 registry_corrupt：
//   - 非 default 世界的名字满足 is_valid_world_name；dir_id 是单独一级路径（非空、不是 "." / ".."、
//     不含 '/' 与 '\\'）；db_path / snapshot_dir 是绝对路径；auto_snapshot 的 interval_hours 在 0–8760；
//   - 布局（世界目录 = <data_dir>/worlds/<dir_id>）。以下三条都在解析符号链接之后判断（含末端悬空的
//     链接），不因字面路径在世界目录里就放行：
//       1. 世界目录本身解析后恰好是（解析后的）<data_dir>/worlds/<dir_id>：不能是指到别的世界目录或
//          其子目录的符号链接（否则删一个世界会删掉它的兄弟），尚不存在时照常通过；
//       2. 非 default 世界的 db_path 严格位于自己的世界目录之下（default 的库保持原地，不受此限）；
//       3. 每个世界（含 default）的 snapshot_dir 严格位于自己的世界目录之下。
//     所以世界目录、snapshots/、world.db 不能是指到世界目录之外的符号链接（指向同一世界目录内
//     别处可以）；路径解析出错（EACCES、ELOOP）同样按越界处理，消息带上原因。
//     数据目录本身可以是符号链接（例如整个 ~/.starling 指到别处）：两侧都解析，比的是同一处。
//     <data_dir>/worlds 解析后也必须在 data_dir 之下；世界目录尚不存在时照常通过。
//   - 每份快照的 kind 是 manual / auto，path 在字面上位于所属世界的 snapshot_dir 之下；
//     kind="manual" 时 reason 必须是 "manual"，kind="auto" 时 reason 必须是 "session" 或 "interval"。
// 布局规则 1–3 的消息写明字段、原路径与解析到的位置，末尾附补救提示（世界目录内的路径不能外链；
// 换盘请整个移动数据目录）。规则与 import_worlds_json 导入前对 worlds.json 的检查是同一份代码。
void validate_world(const RegistryDb& reg, const std::string& world);
// 对注册表里的每个世界跑 validate_world（按 RegistryDb::list 的顺序，遇到第一个违反就抛）。
void validate_registry(const RegistryDb& reg);

// 改一个世界的自动快照配置：读 settings_json，只改 background.auto_snapshot，其余设置原样写回
// （RegistryDb::update_settings）。世界不存在抛 world_not_found，interval_hours 不在 0–8760 抛
// invalid_auto_snapshot；两种情况注册表都不变。settings_json 解析不了时 settings_from_json 的错误照抛。
void set_auto_snapshot(RegistryDb& reg, const std::string& world, const AutoSnapshotConfig& config);

// ---- 世界统计与快照（W2） --------------------------------------------------

struct WorldStats {
    std::int64_t db_bytes = 0;        // 主文件 + -wal 的字节数；文件不存在时为 0
    std::int64_t live_bytes = 0;      // (page_count - freelist_count) * page_size
    std::int64_t statements = 0;      // WHERE tenant_id = tenant
    std::int64_t engrams = 0;         // WHERE tenant_id = tenant
    std::string world_time;           // 见 snapshot_world_time
};
// 只读打开 db_path（mode=ro）。库文件不存在时返回全 0 的统计，不抛异常。
// 表不存在时（库是空的、未迁移），对应计数为 0。
[[nodiscard]] WorldStats world_stats(const std::string& db_path, const std::string& tenant);

// 租户不变式（spec §2.1）：一个世界库里只允许有该世界自己的租户。只读打开 db_path，
// 逐个检查带 tenant_id 列的表，发现 tenant_id 非空且不等于 tenant 的行就抛
// world_tenant_mismatch（detail 点名第一张违规的表）。库文件不存在（新世界，首次激活）
// 时直接通过。库读不出时抛 snapshot_failed（与 world_stats 同一约定）。
void verify_world_tenant(const std::string& db_path, const std::string& tenant);

// 世界时刻水位：source_unit_meta 存在且有非空 observed_at 时取 MAX(observed_at)，
// 否则取 engrams 的 MAX(created_at)，都没有则为空字符串。只读，按 tenant 过滤。
[[nodiscard]] std::string snapshot_world_time(const std::string& db_path,
                                              const std::string& tenant);

struct SnapshotOptions {
    std::string kind = "manual";      // "manual" | "auto"
    std::string reason = "manual";
    std::string note;
    // 向量后端。只有 "sqlite" 的向量在库文件里，其余（zvec）快照会缺向量，拒绝。
    std::string vector_backend = "sqlite";
    // 测试注入用：返回可用字节数。为空时用 std::filesystem::space(dir).available。
    std::function<std::int64_t(const std::filesystem::path&)> free_space;
    // 原样写进条目的 session_id（take_auto_snapshot 的按会话触发填入）。
    std::string session_id;
    // 快照时刻，YYYY-MM-DDTHH:MM:SSZ（可注入的时钟）。非空时快照 id 的时间前缀与条目的
    // created_at 都由它推出，二者描述同一秒；为空时用真实时钟（id 前缀与 created_at 各读一次）。
    // 形状不对抛 invalid_auto_snapshot，在任何 I/O 之前。take_auto_snapshot 填入触发的 now_iso，
    // 判断「间隔到没到」、快照 id 与记录用的是同一个时钟。
    std::string now_iso;
    // 测试注入用：磁盘守卫与快照 id 都定好之后、VACUUM INTO 之前调用（模拟复制前落下的写入或删除）。
    // 为空时不调用。
    std::function<void()> before_vacuum;
};

// 1. 取世界；不存在抛 world_not_found。kind 与 reason 不是 manual/manual、auto/session、
//    auto/interval 之一抛 invalid_snapshot_kind；opts.now_iso 非空且形状不对抛
//    invalid_auto_snapshot；vector_backend 不是 "sqlite" 抛 snapshot_unsupported_backend；保留份数
//    非法抛 invalid_retention。都在任何文件 I/O 之前：快照目录还没建，源库还没打开。
// 1b. 清扫 snapshot_dir 里上次崩溃留下的残留：名字形如快照 id 的 <id>.db.partial，
//    以及没有任何注册项引用的 <id>.db（改名之后、登记之前崩溃的产物，没有元数据，列不出来）。
//    只认快照 id 形状的文件名，别的文件一概不碰；每个删除都经 remove_within（不出
//    snapshot_dir）。清扫失败只记日志，不影响本次快照。
//    调用方必须按世界串行化快照（见文件头「并发」），否则会把并发快照刚改好名、
//    还没登记的文件当成残留。
// 2. 只读打开 db_path，计算 live_bytes；free_space(snapshot_dir) < 3 * live_bytes 时
//    抛 insufficient_disk（不写任何文件）。恰好 3 倍放行。
// 2a. sessions_complete 与 content_mark（W4）在第 5 步、拍出来的快照文件上算（Codex I-2），所以它们
//    恰好描述快照里有什么；复制之前落下的插入或删除都如实反映在快照与标记里，两者不会不一致：
//    sessions_complete = COUNT(DISTINCT session_id) FROM source_unit_meta WHERE tenant_id=?，
//    表不存在记 0；content_mark = "e<count>:<max_rowid>:<id>|s<count>:<max_rowid>:<id>|
//    u<count>:<max_rowid>:<id>"，三段依次是 engrams、statements、source_unit_meta，都按 tenant_id
//    过滤；<id> 是该租户 rowid 最大那一行的主键（engrams.id、statements.id、source_unit_meta 的
//    engram_ref || ':' || clause_id），计数为 0 时为空（"s0:0:"）。表不存在的那段写 "-"，三张表
//    的计数都为 0 时取空串。快照的标记直接与源库上算出的当前标记比较（第 3、4 步的 unchanged），
//    这依赖 VACUUM INTO 保留 rowid：实测成立、由契约测试钉住，SQLite 文档并不承诺（没有
//    INTEGER PRIMARY KEY 的表允许被重排）。坏了的方向是误判有变化、多拍，不会漏拍。
//    原地更新（tick 改 activation）不改变标记，插入与删除会改变它；删掉 rowid 最大的一行再插入
//    （rowid 被复用）、清空租户后重新摄入到相同行数（rowid 从 1 重来）也会改变它（id 不同）。
// 3. id = <UTC 紧凑时刻> + "-" + 4 位随机十六进制；时刻取 opts.now_iso（非空时）或 compact_now()。
//    与该世界已有的 id 重复时重新生成（now_iso 固定时只重抽十六进制部分）。
// 4. 删掉残留的 <id>.db.partial，再用只读连接执行 VACUUM INTO ?（绑定参数）写到 partial。
// 5. 关闭连接，再用只读连接打开 partial，PRAGMA integrity_check 必须为 "ok"，
//    否则删掉 partial 并抛 snapshot_failed。在这条连接上算 world_time、sessions_complete、content_mark。
// 6. fs::rename(partial, <id>.db)，填 size_bytes、created_at（opts.now_iso 非空时就是它，
//    否则取当前时间），session_id 取 opts.session_id。
// 7. 登记到注册表（RegistryDb::put_snapshot，成为该世界最新的一份），然后调用 apply_retention。
//    登记失败时删掉刚改好名的 <id>.db 并抛 snapshot_failed：不留没有登记的文件（原来由调用方存
//    注册表失败后调 discard_snapshot_file 补偿，现在在这里做完）。改名之后、登记之前崩溃留下的
//    <id>.db 由下一次快照的第 1b 步清扫。
//    成功的快照一定返回它的条目：修剪失败（旧快照路径无法解析或删不掉）在这里
//    被吞掉，不外抛。注册表与磁盘仍一致（每个旧快照先删文件再摘注册项），只是
//    暂时多出几份，下一次 take_snapshot 或显式 apply_retention 会重试。
// opts.kind 不是 "manual" / "auto" 时在写任何文件之前抛 invalid_snapshot_kind
// （保留策略只认 "auto"，拼错会悄悄丢掉回滚锚点保护）；reason 不配对同样在这里拒。
// fs::space 返回「未知」（uintmax_t(-1)，FUSE / 网络文件系统）时跳过磁盘守卫。
// 任何 SQLite 错误都转成 snapshot_failed，并保证不留下 partial 文件。
SnapshotEntry take_snapshot(RegistryDb& reg, const std::string& world,
                            const SnapshotOptions& opts);

// 按保留份数 N（RegistryDb::snapshot_retention）修剪该世界的快照，并删除对应的文件，返回被删除的
// id 列表。「最新」「最旧」指 RegistryDb::snapshots 的顺序（登记顺序，后登记的在前）。
// 规则（spec §4.4）：
//   - 总数 <= N：不删。
//   - 超出部分从最旧的开始删，优先删 auto；auto 删完还超出时才删 manual，从最旧的开始。
//   - 但列表里有 auto 时，至少保留最新的 1 份 auto（回滚锚点）。
// 逐个「先删文件、再摘注册项」（每摘一项写一次注册表）。路径解析后越出 snapshot_dir（被篡改的
// 注册项、指向外部的符号链接）时抛 snapshot_failed 并保留该注册项：文件没删，摘掉注册项就再也
// 清不掉它。此前已删的 victim 已从注册表摘掉。摘注册项失败抛 snapshot_failed，文件已删、注册项
// 还在：下一次修剪对不存在的文件照常删除（remove_within 返回 true），再摘一次。
std::vector<std::string> apply_retention(RegistryDb& reg, const std::string& world);

// 删除一份快照（注册项 + 文件）。不存在时抛 snapshot_not_found。文件已经不在了也照删
// 注册项。只删 snapshot_dir 之下的文件：越界时抛 snapshot_failed，注册项保留。
void delete_snapshot(RegistryDb& reg, const std::string& world, const std::string& snapshot_id);

// ---- 自动快照与会话清单（W4） -------------------------------------------------
//
// 设计稿 §4.3（触发）、§7（会话清单）；计划 docs/superpowers/plans/2026-10-28-world-w4-sessions.md。

struct AutoTrigger {
    std::string kind;         // "session" | "interval"
    std::string session_id;   // kind="session" 时必填
    std::string now_iso;      // 当前时刻，YYYY-MM-DDTHH:MM:SSZ（调用方的时钟，可注入）
};

struct AutoSnapshotResult {
    std::optional<SnapshotEntry> taken;   // 拍了快照时是它的条目
    // 没拍时的原因：disabled | known_session | not_due | unchanged | empty | unsupported_backend。
    // unchanged 两种触发都会给（按会话：同一会话已有内容相同的按会话快照）。拍了快照时为空。
    std::string skipped;
    // 修剪卡住、拒绝拍时的原因（第 5 步，Codex I-3 / r2 R2-2），以 "retention cannot prune" 开头，
    // 附修剪的错误。非空时 taken 为空、skipped 为空，注册表可能已被部分修剪（已写入 registry.db）：
    // 调用方把这次当作失败（码 snapshot_failed）。
    std::string refused;
};

// 按触发判断要不要拍一份自动快照，要拍就走 take_snapshot（kind="auto"）。配置取自该世界设置的
// background.auto_snapshot（settings_from_json(rec.settings_json)）。顺序：
//   1. 世界不存在抛 world_not_found；trigger.kind 不是 session / interval、now_iso 解析不了、
//      配置的 interval_hours 不在 0–8760 抛 invalid_auto_snapshot。settings_json 解析不了时
//      settings_from_json 的错误照抛。
//   2. base.vector_backend 不是 "sqlite" → skipped="unsupported_backend"，不抛、不写盘。
//   3. 按会话：on_session 为 false → disabled；session_id 为空抛 invalid_auto_snapshot；库里已有
//      这个会话（只读查 source_unit_meta，表或库文件不存在算没有）→ known_session；
//      注册表里已有一份 kind="auto"、reason="session"、session_id 相同、content_mark 与当前相同
//      （两个空串也算相同）的快照 → unchanged（任意一份都算，不只看最新的 auto：两次重试之间拍了
//      按时间的快照也不重复拍）。调用方在写入之前触发，写入反复失败时会话一直是新的，这一条防止
//      每次重试都拍一份一样的快照、把更早的锚点挤出保留策略；
//      否则拍一份，reason="session"，session_id 填入。
//   4. 按时间：interval_hours 为 0 → disabled；当前 content_mark 为空（库文件不存在也算）→ empty；
//      取该世界 created_at 最新的一份 auto 快照（任意 reason）作为 last：
//        last 的 created_at 能解析、不晚于 now、且 now - created_at < interval_hours 小时 → not_due；
//        last 带 content_mark 且与当前相同 → unchanged；
//      否则拍一份，reason="interval"。没有 last、last 的 created_at 解析不了、last 晚于 now（时钟
//      回拨，或那份快照的时间来自跳到未来的时钟）、last 没有 content_mark（W4 之前的条目），对应
//      那一项都按「需要拍」处理。manual 快照不参与判断。
//   5. 决定要拍之后、拍之前（Codex I-3）：先跑一次 apply_retention（异常接住）；之后该世界的快照数
//      仍 >= snapshot_retention 就返回 refused（原因点名 retention 与修剪的错误），不拍、不写新文件、
//      不抛。否则修剪卡住（最旧的 auto 路径越界或删不掉）时，每次触发都会多登记一份，无人值守地
//      一直涨到磁盘守卫。拒绝时部分修剪的结果已写入注册表：卡住条目之前排着的正常 victim 已删文件、
//      已摘注册项（r2 R2-2），调用方计为失败。
//      修剪成功或本来就少于保留份数时照常拍，新快照让总数至多到保留份数。手动 take_snapshot 不做
//      这一步（W2 语义：有人在场，修剪失败只是暂时多一份），所以封顶只约束自动触发。
// 拍出来的条目 created_at 与 id 的时间前缀都取 trigger.now_iso（经 SnapshotOptions::now_iso）。
// base 的 free_space / before_vacuum / note 照传，kind / reason / session_id / now_iso 由这里决定。
// take_snapshot 的磁盘守卫与保留策略照常生效，失败照常抛出。第 5 步的修剪若删掉了旧条目，这些
// 删除（文件与注册项）在随后拍快照抛错时也已经落盘。
AutoSnapshotResult take_auto_snapshot(RegistryDb& reg, const std::string& world,
                                      const AutoTrigger& trigger,
                                      const SnapshotOptions& base = SnapshotOptions{});

struct SessionRow {
    std::string session_id;
    std::int64_t units = 0;
    std::int64_t speakers = 0;
    std::string first_at;            // MIN(observed_at)；全为 NULL 时为空
    std::string last_at;             // MAX(observed_at)；全为 NULL 时为空
    std::string anchor_snapshot_id;  // 该会话最新一份 auto/session 快照；没有时为空
    std::string anchor_created_at;
};

struct SessionList {
    std::vector<SessionRow> rows;
    std::int64_t total = 0;          // 会话总数（不受 limit 影响）
    std::int64_t null_observed = 0;  // observed_at IS NULL 的 unit 数
    bool truncated = false;          // total > limit
};

// 会话清单（设计稿 §7）：只读打开该世界的库，按 session_id 汇总 source_unit_meta（按 tenant
// 过滤），按 first_at DESC 排序（first_at 为空的排最后，同值按 session_id 升序），取前 limit 行。
// 锚点取注册表里 kind="auto"、reason="session"、session_id 相同的快照中 created_at 最新的一份。
// 世界不存在抛 world_not_found；limit 不在 1–1000 抛 invalid_auto_snapshot；库文件或表不存在
// 返回空清单（total=0）；库读不出抛 snapshot_failed。
[[nodiscard]] SessionList list_sessions(const RegistryDb& reg, const std::string& world,
                                        std::int64_t limit);

// ---- 回滚与归档（W3） ------------------------------------------------------
//
// 设计稿 §5.1 语义 A（丢掉后续）。世界 home = <data_dir>/worlds/<dir_id>（data_dir 即
// reg.data_dir()，dir_id 取自注册表；与 RegistryDb::create / remove 同一推导；snapshot_dir 不参与），
// 归档目录为 <世界 home>/archive。回滚不写注册表。
//
// 归档操作（回滚建硬链接、列出、清理）之前都先 fail closed，此时什么都还没建、没删：
//   a. <data_dir>/worlds 解析符号链接后仍在 data_dir 之下，否则 registry_corrupt
//      （与 validate_world 同一检查）；
//   b. dir_id 是单独一级路径（非空、不是 "." / ".."、不含 '/' 与 '\\'），世界 home 解析符号链接后
//      严格在 <data_dir>/worlds 之下，并且恰好是解析后的 <data_dir>/worlds/<dir_id>（与 validate_world
//      的布局规则 1 相同：不经校验直接调用时，worlds/<sim-1> -> worlds/<sim-2> 不能让 sim-1 的回滚、
//      列出、清理落到 sim-2 上），否则 snapshot_failed（与 RegistryDb::remove 同一边界）；
//   c. archive/ 解析符号链接后严格在世界 home 之下，否则 snapshot_failed。
// 回滚只在 a、b 通过之后才 create_directories(archive)，所以世界目录之外什么都不会被建出来。

struct RollbackOptions {
    // 向量后端。只有 "sqlite" 的向量在库文件里，其余（zvec）恢复不出完整的世界，拒绝（spec §12）。
    std::string vector_backend = "sqlite";
    // 测试注入用：返回可用字节数。为空时用 std::filesystem::space(dir).available。
    std::function<std::int64_t(const std::filesystem::path&)> free_space;
    // 测试注入用：归档名建好之后、原子替换之前（第 10 步与第 11 步之间）调用，参数是 partial
    // 的路径。抛异常或删掉 partial 即可模拟「替换失败」。为空时不调用。
    std::function<void(const std::filesystem::path& staged)> before_replace;
};

struct RollbackResult {
    std::string archive_path;   // 被替换的库现在的位置；库原本不存在时为空
    std::int64_t restored_bytes = 0;
};

// 回滚的预检：只做 rollback_world 第 1–4 步的只读检查，顺序、错误码与 rollback_world 相同：
//   world_not_found → snapshot_not_found → snapshot_unsupported_backend → 快照路径越出
//   snapshot_dir（snapshot_failed）→ 快照文件缺失或不是普通文件（snapshot_corrupt）→ 世界 home
//   的 a、b 检查（registry_corrupt / snapshot_failed）→ insufficient_disk（2 ×）。
// 什么都不动：不清扫、不复制、不建目录。给调用方在停引擎之前先拒掉「不改输入、重试也过不了」
// 的请求（WorldManager.rollback 在切换窗口之前调它）。通过不代表回滚一定成功：rollback_world
// 自己再查一遍，以它为准（预检之后状态可能变了）。非 WorldStoreError 的异常转成 snapshot_failed。
void check_rollback(const RegistryDb& reg, const std::string& world,
                    const std::string& snapshot_id, const RollbackOptions& opts);

// 把世界 world 的库替换成快照 snapshot_id 的内容（spec §5.1 语义 A）。
// 别的连接：第 8 步的探测（BEGIN EXCLUSIVE，busy_timeout 0）看得见哪些连接取决于库的日志模式
// （在生产库副本上实测）：WAL 库上读过库的闲置连接（只读、读写都算）与开着的读事务都会让它
// world_in_use；rollback-journal（delete）模式的库上只有开着的读事务会；只打开、从没读过的
// 连接两种模式都看不见。恢复出来的库是 VACUUM INTO 的产物，引擎打开它之前一直是 delete 模式。
// 所以探测只是纵深防御：别的进程对 dashboard 管的库开写连接不在支持的使用方式之内；进程内的
// 安全靠调用方（Python 侧：打开该库的每条路径都在引擎租约之内，回滚等租约归零；旧 Runtime 的
// 弱引用失效了才调这里；切换遗留的 Runtime 登记在案，开着这个库就拒绝）。
// 不变式：库路径上任何时刻都是一份完整的库（替换前是旧库，替换后是新库），从不为空。
// 归档用硬链接建出来，替换是 rename(partial, 库) 一步原子完成。
// 顺序（第 1–4 步与 check_rollback 是同一份代码）：
//   1. 取世界（world_not_found）与快照（snapshot_not_found）。opts.vector_backend 不是
//      "sqlite" 抛 snapshot_unsupported_backend。都在任何文件 I/O 之前。
//   2. 快照路径解析符号链接后必须严格在该世界 snapshot_dir 之下（snapshot_failed）。
//   3. 快照文件缺失或不是普通文件 → snapshot_corrupt。
//   4. 世界 home 的 a、b 两项检查（见上）；free_space(库所在目录) < 2 × 快照文件大小 →
//      insufficient_disk。
//   4a. 清扫库所在目录里上次回滚崩溃留下的 partial：名字严格为
//      `.<库文件名>.rollback-<16 位小写 hex>.partial` 的普通文件（跳过符号链接），每个删除都经
//      remove_within。再摘掉 archive 里与库是同一个文件（fs::equivalent）的归档名：上次回滚死在
//      第 9 步之后、第 11 步之前时留下的现役库第二个名字（删掉它只少一个名字）。清扫失败只记
//      日志，不影响本次回滚。调用方必须按世界串行化回滚。
//   5. 把快照复制到库所在目录的 `.<库文件名>.rollback-<16 hex>.partial`。
//   6. 只读打开 partial，PRAGMA integrity_check 必须为 "ok"；打不开、不是 SQLite 同样算
//      （snapshot_corrupt）。校验的是要装上去的那份字节，不是快照文件。
//   7. verify_world_tenant(partial, 世界租户)（world_tenant_mismatch）。
//   8. 库路径存在但不是普通文件（例如符号链接）→ snapshot_failed。库旁边的 `-wal` 或
//      `-journal` 存在且非空（或不是普通文件）→ snapshot_failed（还有未合并的写入、写连接或
//      热日志）；库不存在时同样检查，否则残留的 WAL / 热日志会被 SQLite 重放到恢复出来的库上。
//      `-shm` 存在但不是普通文件（目录、符号链接）→ snapshot_failed；大小不论（wal-index 不承载
//      持久数据）。
//      库存在时：前 16 字节必须是 "SQLite format 3\0"，否则 snapshot_failed（default 世界的
//      db_path 不受布局约束）；partial 沿用库的权限位；最后紧挨着第 9 步探测别的连接：新开一条
//      读写连接（不 CREATE），busy_timeout 0，`PRAGMA locking_mode=EXCLUSIVE; BEGIN EXCLUSIVE`
//      拿不到锁（SQLITE_BUSY / SQLITE_LOCKED）→ world_in_use，其他探测失败 → snapshot_failed。
//      探测排在 -wal / -journal 检查之后，不会把残留的 WAL 或热日志重放进旧库。
//   9. 库存在时再做一遍 a、b，然后 create_directories(<世界 home>/archive)（c：解析符号链接后
//      必须仍在世界 home 之下），用 create_hard_link 给库建归档名
//      `<archive>/<库文件名>.pre-rollback-<compact_now>-<4 hex>`（名字被占用就重抽）。建不了
//      （EXDEV、文件系统不支持硬链接、权限）→ snapshot_failed，什么都没变。EXDEV 的错误信息写明
//      归档目录与库不在同一个文件系统、要把数据目录挪到库旁边（只在数据目录在另一个卷、default
//      世界的库仍在原卷时出现；不退回复制）。
//   10. 删掉库旁边的空 `-wal` / `-journal` 和 `-shm`（只读读者会留下 32 KB 的 `-shm` 与
//      0 字节的 `-wal`）。在替换之前删：它们不带状态（-wal / -journal 已确认为空，-shm 由 SQLite
//      按主文件加 WAL 重建），替换随后失败时旧库少了它们照常可用；替换之后再删，会有一段时间
//      旧库的 -shm / -wal 挂在新库旁边。
//   11. rename(partial, 库路径)，原子替换。旧 inode 只剩归档名这一个名字。
// 第 1–9 步任何失败：目标库原样不动，partial 删掉。
// 第 10–11 步失败：库路径上仍是旧库，摘掉归档名（经 remove_within），删掉 partial，抛
// snapshot_failed。归档名摘不掉时它是现役库的第二个名字：记 stderr，错误信息写明不能用写
// 连接打开它（清理它只删一个名字，无害）。
// 不改注册表。所有非 WorldStoreError 的异常都转成 snapshot_failed。
RollbackResult rollback_world(const RegistryDb& reg, const std::string& world,
                              const std::string& snapshot_id, const RollbackOptions& opts);

struct ArchiveEntry {
    std::string name;            // 文件名，例如 "world.db.pre-rollback-20261026T120000Z-ab12"
    std::int64_t size_bytes = 0;
};

// 列出 <世界 home>/archive 下文件名形如 "*.pre-rollback-<YYYYMMDDTHHMMSSZ>-<4 hex>" 的
// 普通文件（跳过符号链接），按名字降序（最新在前）。目录不存在时返回空。与现役库是同一个文件
// （fs::equivalent）的条目不是归档，跳过（不计个数、不计字节、不清理）。
// 不存在的世界抛 world_not_found；a、b、c 检查（见上）不通过、或读不出时抛 registry_corrupt /
// snapshot_failed。
[[nodiscard]] std::vector<ArchiveEntry> list_rollback_archives(const RegistryDb& reg,
                                                             const std::string& world);

// 删除 list_rollback_archives 列出的全部文件，每个都经 remove_within(archive 目录, 文件)。
// 返回删除的个数。确认越界的文件不删，抛 snapshot_failed。
std::size_t purge_rollback_archives(const RegistryDb& reg, const std::string& world);

}  // namespace starling::governance
