#pragma once
#include "starling/persistence/connection.hpp"
#include <string>
#include <string_view>
#include <vector>

namespace starling::reconsolidation {

// 自适应超时 (分钟): 默认 30, clamp [5, 360]. modality COMMITS 长 / ASSUMES 短.
// 高频 trigger_freq_per_hour >= 3 → 强制 5. (§16.3-4)
int adaptive_timeout_minutes(std::string_view modality, int trigger_freq_per_hour);

struct OpenResult { bool opened=false; bool appended=false; std::string close_deadline; };

// 开窗或追加证据 (窗口锁: (tenant_id, stmt_id) PK).
// 无活跃窗口 → INSERT 新窗口 (close_deadline = now + adaptive_timeout(modality, freq)), opened=true.
// 有活跃 (status='open') 窗口 → INSERT pending_evidence (防抖) + force_close_trigger_count+1;
//   pending count > 100 → 删最旧 (FIFO) + evicted_count+1;
//   force_close_trigger_count >= 10 → status='closed' (强制 close); appended=true.
OpenResult open_or_append(persistence::Connection& conn,
                          std::string_view stmt_id, std::string_view tenant_id,
                          std::string_view event_id, std::string_view event_type,
                          std::string_view payload_hash, double weight,
                          std::string_view modality, std::string_view now_iso);

struct DueWindow { std::string stmt_id; std::string tenant_id; };

// 列出 close_deadline <= now 的 open 窗口.
std::vector<DueWindow> due_windows(persistence::Connection& conn, std::string_view now_iso);

constexpr int kPendingEvidenceMax = 100;
constexpr int kForceCloseTriggerCount = 10;

// Delete only this window's pending evidence after closure.
void clear_pending_evidence(persistence::Connection& conn,
                            std::string_view stmt_id, std::string_view tenant_id);

}  // namespace starling::reconsolidation
