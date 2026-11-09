#pragma once
// SqliteStatementStore —— StatementStore 的 local-store(SQLite)backend
// (P3.b1 phase 2)。绑定 Connection&(写的实际单位):replay/recon/bus 的写者
// 都持有 Connection&(常在已开事务中),故路由后与原事务同连接、行为不变。
#include "starling/persistence/connection.hpp"
#include "starling/store/statement_store.hpp"

namespace starling::store {

class SqliteStatementStore : public StatementStore {
public:
    explicit SqliteStatementStore(persistence::Connection& conn) : conn_(conn) {}

    int mark_consolidated(const std::vector<std::string>&, std::string_view,
                          std::string_view) override;
    int reinforce(const std::vector<std::string>&, std::string_view,
                  std::string_view) override;
    int bump_replay_count(const std::vector<std::string>&, std::string_view,
                          std::string_view) override;
    int enter_reconsolidating(std::string_view, std::string_view) override;
    int restore_consolidated(std::string_view, std::string_view) override;
    int force_consolidate_pending_review() override;
    int archive(const std::vector<std::string>&, std::string_view,
                std::string_view, std::optional<std::string>) override;
    void apply_mild_correction(std::string_view, std::string_view, double,
                               std::string_view, std::string_view) override;
    void apply_mild_contradict(std::string_view, std::string_view, double,
                               std::string_view) override;
    int archive_nonterminal(std::string_view, std::string_view,
                            std::string_view) override;
    int forget(std::string_view, std::string_view, std::string_view) override;
    // 片 6 干预集:review_requested → approved(人工审批清场)。守卫幂等、tenant-scoped。
    // 仅 SqliteStatementStore 有(memoryops::approve_review 直构造本类调用);不上抽象接口。
    // reject 不走这里 —— reject = forget(→forgotten 终态,见 forget())。
    int approve_review(std::string_view id, std::string_view tenant,
                       std::string_view updated_at);
    // #38-C: set the consolidation LLM's natural-language summary on a gist row.
    // BEST-EFFORT / never throws — the gist is already written+committed when this
    // runs and the summary is a non-invariant prose field (not an immutable-trigger
    // column); on failure the column stays NULL (observable). SqliteStatementStore-
    // only (like approve_review); not on the abstract interface.
    void set_consolidation_summary(std::string_view stmt_id, std::string_view tenant,
                                   std::string_view summary);
    // #38-C Phase 4 (gating): promote a verified gist volatile→consolidated +
    // →approved (the only path that makes a gist live/retrievable). STATE-GUARDED:
    // acts only on a still-inert consolidation_abstract gist (consolidation_state=
    // 'volatile'), so if pipeline arbitration already archived the gist on a
    // conflict, this is a no-op (conflict ⇒ no auto-consolidate). Returns rows
    // changed (>0 = promoted). SqliteStatementStore-only.
    int promote_gist_to_consolidated(std::string_view stmt_id, std::string_view tenant,
                                     std::string_view now_iso);
    int set_confidence_consolidated(std::string_view, std::string_view,
                                     double) override;
    void inherit_salience(std::string_view, std::string_view, double,
                          std::string_view) override;
    void insert_arbitrated_fork(const ArbitratedFork&) override;

private:
    persistence::Connection& conn_;
};

}  // namespace starling::store
