"""变体缓存存储。"""

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from .contracts import TemplateVariant

_COLUMNS = """variant_id, scope, group_id, module, template_text,
             template_fingerprint, event_fingerprint,
             param_positions_json,
             position_schema, structural_signature_json,
             parser_revision, target_definition_revision,
             rule_revision, matched_template_id,
             similarity_score, encoder_revision, state, created_at"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS variants (
    variant_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    group_id TEXT NOT NULL,
    module TEXT NOT NULL,
    template_text TEXT NOT NULL,
    template_fingerprint TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    param_positions_json TEXT NOT NULL,
    position_schema TEXT NOT NULL,
    structural_signature_json TEXT NOT NULL,
    parser_revision TEXT NOT NULL,
    target_definition_revision TEXT NOT NULL,
    rule_revision TEXT NOT NULL,
    matched_template_id TEXT,
    similarity_score REAL,
    encoder_revision TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(scope, module, event_fingerprint,
           position_schema, parser_revision)
);
CREATE INDEX IF NOT EXISTS idx_variants_lookup
    ON variants(scope, module, event_fingerprint, state);
"""

_SCHEMA_NEGATIVE = """
CREATE TABLE IF NOT EXISTS negative_cache (
    scope TEXT NOT NULL,
    module TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    group_id TEXT NOT NULL,
    values_json TEXT NOT NULL,
    target_definition_revision TEXT NOT NULL,
    rule_revision TEXT NOT NULL,
    parser_revision TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(scope, module, event_fingerprint, parser_revision)
);
CREATE INDEX IF NOT EXISTS idx_negative_lookup
    ON negative_cache(scope, module, event_fingerprint);
"""

_SCHEMA_VERSION = 4

class VariantStore:
    """线程安全 SQLite 变体存储（进程内一把写锁 + WAL）。"""

    def __init__(self, db_path, scope: str = "main",
                 read_only: bool = False):
        if scope not in ("main", "shadow"):
            raise ValueError("scope must be 'main' or 'shadow'")
        self.db_path = str(db_path)
        self.scope = scope
        self.read_only = read_only
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, timeout=30.0,
                                      check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self):
        """建表 + schema 版本校验（不可迁移格式重建：变体为可再学习
        缓存，重建不丢失规则，冷路径可再生）。"""
        try:
            ver = self._conn.execute(
                "PRAGMA user_version").fetchone()[0]
        except sqlite3.Error:
            ver = 0
        has_table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='variants'").fetchone()
        if has_table and ver != _SCHEMA_VERSION:
            if ver == 2:
                self._migrate_v2_to_v3()
            elif ver == 3:
                pass   # v3 -> v4 为加表迁移，变体数据保留
            else:
                # 旧 schema（无 event_fingerprint）-> 重建
                self._conn.execute("DROP TABLE variants")
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_SCHEMA_NEGATIVE)
        self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._conn.commit()

    def _migrate_v2_to_v3(self):
        """v2 -> v3：同事件多快照行去重（保留最新一行）。

        v2 唯一键含模板快照指纹，同事件在不同快照下各存一行；
        v3 按事件一行。迁移按 created_at 降序插入，INSERT OR
        IGNORE 保留最新快照（模板演化最深 + 最新校验的位置）。
        """
        try:
            self._conn.executescript(f"""
                BEGIN;
                CREATE TABLE variants_v3 (
                    variant_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    template_text TEXT NOT NULL,
                    template_fingerprint TEXT NOT NULL,
                    event_fingerprint TEXT NOT NULL,
                    param_positions_json TEXT NOT NULL,
                    position_schema TEXT NOT NULL,
                    structural_signature_json TEXT NOT NULL,
                    parser_revision TEXT NOT NULL,
                    target_definition_revision TEXT NOT NULL,
                    rule_revision TEXT NOT NULL,
                    matched_template_id TEXT,
                    similarity_score REAL,
                    encoder_revision TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(scope, module, event_fingerprint,
                           position_schema, parser_revision)
                );
                INSERT OR IGNORE INTO variants_v3 ({_COLUMNS})
                    SELECT {_COLUMNS}
                    FROM variants ORDER BY created_at DESC;
                DROP TABLE variants;
                ALTER TABLE variants_v3 RENAME TO variants;
                COMMIT;
            """)
        except sqlite3.Error:
            self._conn.rollback()
            self._conn.execute("DROP TABLE IF EXISTS variants")
            self._conn.commit()

    # ------------------------------------------------------------

    def register(self, variant: TemplateVariant,
                 rule_revision: str = "") -> Tuple[bool, Optional[str]]:
        """登记变体（按事件幂等去重，同事件刷新为最新快照）。

        唯一键冲突（同事件已存在）时：刷新该行为本次冷路径校验
        结果——位置/签名/模板快照/variant_id 更新为最新，disabled
        重新学习为 executable。返回 (是否写入/生效, 说明)。
        只读模式不写入，返回 (False, "read_only")。
        """
        if self.read_only:
            return False, "read_only"
        try:
            with self._lock:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO variants
                       (variant_id, scope, group_id, module, template_text,
                        template_fingerprint, event_fingerprint,
                        param_positions_json,
                        position_schema, structural_signature_json,
                        parser_revision, target_definition_revision,
                        rule_revision, matched_template_id,
                        similarity_score, encoder_revision, state,
                        created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (variant.variant_id, self.scope, variant.group_id,
                     variant.module, variant.template_text,
                     variant.template_fingerprint,
                     variant.event_fingerprint,
                     json.dumps(variant.param_positions,
                                ensure_ascii=False),
                     variant.position_schema,
                     json.dumps(variant.structural_signature,
                                ensure_ascii=False),
                     variant.parser_revision,
                     variant.target_definition_revision,
                     rule_revision,
                     None if variant.matched_template_id is None
                     else str(variant.matched_template_id),
                     variant.similarity_score, variant.encoder_revision,
                     variant.state, time.time()))
                if cur.rowcount == 0:
                    # 同事件已存在：刷新为本次校验结果（disabled
                    # 亦重新学习为 executable）
                    cur2 = self._conn.execute(
                        """UPDATE variants
                           SET state='executable',
                               param_positions_json=?,
                               structural_signature_json=?,
                               template_text=?,
                               template_fingerprint=?,
                               variant_id=?,
                               group_id=?, target_definition_revision=?,
                               rule_revision=?, similarity_score=?,
                               encoder_revision=?
                           WHERE scope=? AND module=?
                             AND event_fingerprint=?
                             AND position_schema=?
                             AND parser_revision=?""",
                        (json.dumps(variant.param_positions,
                                    ensure_ascii=False),
                         json.dumps(variant.structural_signature,
                                    ensure_ascii=False),
                         variant.template_text,
                         variant.template_fingerprint,
                         variant.variant_id,
                         variant.group_id,
                         variant.target_definition_revision,
                         rule_revision, variant.similarity_score,
                         variant.encoder_revision,
                         self.scope, variant.module,
                         variant.event_fingerprint,
                         variant.position_schema,
                         variant.parser_revision))
                    self._conn.commit()
                    if cur2.rowcount > 0:
                        return True, None
                    return False, "duplicate"
                self._conn.commit()
                return True, None
        except sqlite3.Error as exc:
            return False, f"sqlite:{exc}"

    def lookup(self, module: str,
               event_fingerprint: str) -> Optional[TemplateVariant]:
        """按事件指纹查可执行变体（state=executable）。

        事件指纹 = 清洗值后的有序 token 序列哈希：值变化不改变
        指纹 -> 同事件缓存命中；解析模板快照演化不影响查找
        （快照不是身份）。
        """
        try:
            row = self._conn.execute(
                """SELECT variant_id, group_id, module, template_text,
                          template_fingerprint, event_fingerprint,
                          param_positions_json,
                          position_schema, structural_signature_json,
                          parser_revision, target_definition_revision,
                          matched_template_id, similarity_score,
                          encoder_revision, state
                   FROM variants
                   WHERE scope=? AND module=? AND event_fingerprint=?
                     AND state='executable'""",
                (self.scope, module, event_fingerprint)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return self._row_to_variant(row)

    def register_negative(self, module: str,
                          event_fingerprint: str, group_id: str,
                          values: dict, target_definition_revision: str,
                          rule_revision: str, parser_revision: str
                          ) -> bool:
        """登记负缓存（路由成功但位置映射失败的事件结构）。

        values: {rule_id: 抽取值或 None}——存入冷路径的实际取值，
        命中时用当前规则重判（黑名单变化自动生效）。
        """
        if self.read_only:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT OR REPLACE INTO negative_cache
                       (scope, module, event_fingerprint, group_id,
                        values_json, target_definition_revision,
                        rule_revision, parser_revision, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (self.scope, module, event_fingerprint, group_id,
                     json.dumps(values, ensure_ascii=False),
                     target_definition_revision, rule_revision,
                     parser_revision, time.time()))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False

    def lookup_negative(self, module: str, event_fingerprint: str,
                        parser_revision: str) -> Optional[dict]:
        """查负缓存（按事件指纹 + parser_revision）。"""
        try:
            row = self._conn.execute(
                """SELECT group_id, values_json,
                          target_definition_revision, rule_revision
                   FROM negative_cache
                   WHERE scope=? AND module=?
                     AND event_fingerprint=? AND parser_revision=?""",
                (self.scope, module, event_fingerprint,
                 parser_revision)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {"group_id": row[0], "values": json.loads(row[1]),
                "target_definition_revision": row[2],
                "rule_revision": row[3]}

    def lookup_all(self, module: Optional[str] = None
                   ) -> List[TemplateVariant]:
        """全部可执行变体（重启后重建匹配索引用）。"""
        sql = """SELECT variant_id, group_id, module, template_text,
                        template_fingerprint, event_fingerprint,
                        param_positions_json,
                        position_schema, structural_signature_json,
                        parser_revision, target_definition_revision,
                        matched_template_id, similarity_score,
                        encoder_revision, state
                 FROM variants WHERE scope=? AND state='executable'"""
        args: Tuple = (self.scope,)
        if module is not None:
            sql += " AND module=?"
            args = (self.scope, module)
        try:
            rows = self._conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        return [self._row_to_variant(r) for r in rows]

    def invalidate(self, variant_id: str,
                   reason: str = "") -> Tuple[bool, Optional[str]]:
        """使变体失效（state=disabled）。签名校验失败等场景。

        只读模式不落库（返回成功仅表示本次不走缓存）。
        """
        if self.read_only:
            return True, reason or "read_only_noop"
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE variants SET state='disabled' "
                    "WHERE scope=? AND variant_id=?",
                    (self.scope, variant_id))
                self._conn.commit()
            return True, reason or None
        except sqlite3.Error as exc:
            return False, f"sqlite:{exc}"

    def stats(self) -> dict:
        try:
            row = self._conn.execute(
                "SELECT state, COUNT(*) FROM variants WHERE scope=? "
                "GROUP BY state", (self.scope,)).fetchall()
            return {"scope": self.scope,
                    "by_state": {r[0]: r[1] for r in row},
                    "db_path": self.db_path}
        except sqlite3.Error as exc:
            return {"scope": self.scope, "error": str(exc)}

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------

    @staticmethod
    def _row_to_variant(row) -> TemplateVariant:
        (vid, gid, module, text, fp, event_fp, pos_json, schema,
         sig_json, parser_rev, target_rev, matched_tid, score,
         enc_rev, state) = row
        return TemplateVariant(
            variant_id=vid, group_id=gid, module=module,
            template_text=text, template_fingerprint=fp,
            event_fingerprint=event_fp,
            param_positions=json.loads(pos_json),
            position_schema=schema,
            structural_signature=json.loads(sig_json),
            parser_revision=parser_rev,
            target_definition_revision=target_rev,
            matched_template_id=matched_tid,
            similarity_score=score, encoder_revision=enc_rev,
            state=state)

def new_variant_id() -> str:
    return uuid.uuid4().hex[:16]
