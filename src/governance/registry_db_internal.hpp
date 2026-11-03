#pragma once

// registry_db_internal.hpp — registry_db.cpp 给导入器（registry_import.cpp）用的内部入口。
// 不属于公开 API，只供 src/governance 内部包含。

#include "starling/governance/registry_db.hpp"

#include <filesystem>
#include <string>
#include <vector>

namespace starling::governance::internal {

struct ImportedWorld {
    // 用到的字段：name、dir_id、tenant、db_path、snapshot_dir、label、created_at、settings_json。
    WorldRecord record;
    std::vector<SnapshotEntry> snapshots;   // 最新在前（与 RegistryDb::snapshots 同序）
};

// 在 file（调用方保证它不存在）建一份全新的注册表：建表、写 meta（schema_version、default_world、
// snapshot_retention），在一个事务里写入全部世界与快照（快照按登记顺序，最旧的先写），提交之后把
// 日志模式切回 DELETE 再关闭，磁盘上只剩 file 一个文件，可以直接改名。RegistryDb::open 下次打开时
// 再切成 WAL。任何 SQLite 错误（例如同一世界两份快照 id 相同）抛 registry_corrupt；调用方负责
// 失败时删掉 file 与它的旁车。
void write_fresh_registry(const std::filesystem::path& file, const std::string& default_world,
                          int snapshot_retention, const std::vector<ImportedWorld>& worlds);

}  // namespace starling::governance::internal
