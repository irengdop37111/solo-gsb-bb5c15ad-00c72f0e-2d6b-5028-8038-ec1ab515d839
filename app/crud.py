"""数据读写与配置校验。"""

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from app import engine as eng
from app.models import DecisionSnapshot, FlagConfig, FlagConfigVersion, UserOverride
from app.schemas import FlagUpsertRequest, OverrideUpsertRequest, RuleIn, VariantIn


class NotFound(Exception):
    """开关不存在。"""


class VersionNotFound(Exception):
    """指定的历史版本不存在。"""


class VersionConflict(Exception):
    """expected_current_version 与实际当前版本不一致（期望版本已过期）。"""


class VariantNotFound(Exception):
    """variant_key 不存在于开关当前配置。"""


def parse_rules(raw: str) -> list[eng.Rule]:
    return [eng.Rule(**item) for item in json.loads(raw or "[]")]


def parse_variants(raw: str) -> list[eng.Variant]:
    return [eng.Variant(**item) for item in json.loads(raw or "[]")]


def upsert_flag(db: Session, payload: FlagUpsertRequest) -> FlagConfig:
    """创建开关；已存在时版本号 +1 并整体覆盖规则/变体。

    同一事务中向 flag_config_versions 追加一份完整不可变快照。
    """
    rules_json = json.dumps(
        [r.model_dump() for r in sorted(payload.rules, key=lambda x: x.priority)],
        ensure_ascii=False,
    )
    variants_json = json.dumps([v.model_dump() for v in payload.variants], ensure_ascii=False)

    flag = db.scalar(select(FlagConfig).where(FlagConfig.flag_key == payload.flag_key))
    if flag is None:
        flag = FlagConfig(
            flag_key=payload.flag_key,
            description=payload.description,
            rules_json=rules_json,
            variants_json=variants_json,
            version=1,
        )
        db.add(flag)
    else:
        flag.description = payload.description
        flag.rules_json = rules_json
        flag.variants_json = variants_json
        flag.version += 1
    db.add(
        FlagConfigVersion(
            flag_key=payload.flag_key,
            version=flag.version,
            description=payload.description,
            rules_json=rules_json,
            variants_json=variants_json,
        )
    )
    db.commit()
    db.refresh(flag)
    return flag


def rollback_flag(
    db: Session, flag_key: str, target_version: int, expected_current_version: int
) -> FlagConfig:
    """把指定历史版本复制为新的当前版本（回滚发布）。

    仅当开关存在、实际当前版本等于 expected_current_version、且
    target_version 历史版本存在时，在**同一事务**中：
    1. 把目标历史版本的完整配置追加为新的不可变版本（version + 1）；
    2. 更新当前配置的 description / rules / variants。

    历史版本一律只增不改；期望版本过期抛 VersionConflict（不写入、不提交），
    目标版本不存在抛 VersionNotFound。
    """
    flag = db.scalar(select(FlagConfig).where(FlagConfig.flag_key == flag_key))
    if flag is None:
        raise NotFound(flag_key)
    if flag.version != expected_current_version:
        raise VersionConflict(
            f"开关 {flag_key!r} 当前版本为 {flag.version}，与期望版本 "
            f"{expected_current_version} 不一致"
        )
    target = get_flag_version(db, flag_key, target_version)
    if target is None:
        raise VersionNotFound(
            f"开关 {flag_key!r} 没有版本 {target_version}"
        )

    flag.description = target.description
    flag.rules_json = target.rules_json
    flag.variants_json = target.variants_json
    flag.version += 1
    db.add(
        FlagConfigVersion(
            flag_key=flag_key,
            version=flag.version,
            description=target.description,
            rules_json=target.rules_json,
            variants_json=target.variants_json,
        )
    )
    db.commit()
    db.refresh(flag)
    return flag


def get_flag_or_none(db: Session, flag_key: str) -> FlagConfig | None:
    return db.scalar(select(FlagConfig).where(FlagConfig.flag_key == flag_key))


def list_flags(db: Session) -> Sequence[FlagConfig]:
    return db.scalars(select(FlagConfig).order_by(FlagConfig.flag_key)).all()


def delete_flag(db: Session, flag_key: str) -> bool:
    flag = get_flag_or_none(db, flag_key)
    if flag is None:
        return False
    # 版本历史随开关一起删除，避免删除后重建同 key 开关时版本号冲突
    db.execute(delete(FlagConfigVersion).where(FlagConfigVersion.flag_key == flag_key))
    # 用户级 override 同样级联删除，避免重建同名开关后旧强制记录复活
    db.execute(delete(UserOverride).where(UserOverride.flag_key == flag_key))
    db.delete(flag)
    db.commit()
    return True


def list_flag_versions(db: Session, flag_key: str) -> Sequence[FlagConfigVersion]:
    return db.scalars(
        select(FlagConfigVersion)
        .where(FlagConfigVersion.flag_key == flag_key)
        .order_by(FlagConfigVersion.version)
    ).all()


def get_flag_version(db: Session, flag_key: str, version: int) -> FlagConfigVersion | None:
    return db.scalar(
        select(FlagConfigVersion).where(
            FlagConfigVersion.flag_key == flag_key,
            FlagConfigVersion.version == version,
        )
    )


def backfill_missing_versions(db: Session) -> int:
    """为缺少版本记录的开关补齐当前版本快照（兼容本功能上线前已有的数据）。"""
    added = 0
    for flag in list_flags(db):
        if get_flag_version(db, flag.flag_key, flag.version) is None:
            db.add(
                FlagConfigVersion(
                    flag_key=flag.flag_key,
                    version=flag.version,
                    description=flag.description,
                    rules_json=flag.rules_json,
                    variants_json=flag.variants_json,
                    created_at=flag.updated_at,
                )
            )
            added += 1
    if added:
        db.commit()
    return added


def flag_to_response(flag: FlagConfig) -> dict:
    rules = [RuleIn(**item) for item in json.loads(flag.rules_json)]
    variants = [VariantIn(**item) for item in json.loads(flag.variants_json)]
    return {
        "flag_key": flag.flag_key,
        "description": flag.description,
        "version": flag.version,
        "variants": variants,
        "rules": rules,
        "created_at": flag.created_at,
        "updated_at": flag.updated_at,
    }


def variant_keys_of(flag: FlagConfig) -> set[str]:
    return {item["key"] for item in json.loads(flag.variants_json)}


# ---------------------------------------------------------------------------
# 按用户临时强制变体（override）
# ---------------------------------------------------------------------------


def _naive_utc(dt: datetime) -> datetime:
    """统一成 naive UTC 存储，与 SQLite 读出形态及 _utcnow 写入保持一致。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def get_override(db: Session, flag_key: str, user_id: str) -> UserOverride | None:
    return db.scalar(
        select(UserOverride).where(
            UserOverride.flag_key == flag_key,
            UserOverride.user_id == user_id,
        )
    )


def get_active_override(
    db: Session, flag: FlagConfig, user_id: str
) -> UserOverride | None:
    """返回当前判定应生效的 override，否则 None。

    仅当记录未过期（expires_at 为空或晚于当前 UTC 时间）**且**强制变体仍
    存在于开关当前配置时生效；到期或变体已被配置移除的记录不生效。
    """
    record = get_override(db, flag.flag_key, user_id)
    if record is None:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if record.expires_at is not None:
        # SQLite 读出为 naive UTC；兼容其他后端可能给出 aware datetime
        expires = record.expires_at
        if expires.tzinfo is not None:
            expires = expires.astimezone(timezone.utc).replace(tzinfo=None)
        if expires <= now:
            return None
    if record.variant_key not in variant_keys_of(flag):
        return None
    return record


def upsert_override(
    db: Session, flag: FlagConfig, user_id: str, payload: OverrideUpsertRequest
) -> UserOverride:
    """写入或整体替换某开关下某用户的临时强制变体。

    variant_key 必须存在于开关当前配置，否则抛 VariantNotFound；
    expires_at 规范化为 naive UTC（None 表示长期有效）。
    """
    if payload.variant_key not in variant_keys_of(flag):
        raise VariantNotFound(payload.variant_key)

    record = get_override(db, flag.flag_key, user_id)
    expires = _naive_utc(payload.expires_at) if payload.expires_at is not None else None
    if record is None:
        record = UserOverride(
            flag_key=flag.flag_key,
            user_id=user_id,
            variant_key=payload.variant_key,
            reason=payload.reason,
            expires_at=expires,
        )
        db.add(record)
    else:
        record.variant_key = payload.variant_key
        record.reason = payload.reason
        record.expires_at = expires
    db.commit()
    db.refresh(record)
    return record


def delete_override(db: Session, flag_key: str, user_id: str) -> bool:
    """撤销指定 override；记录不存在同样视为成功（幂等），返回是否删除了行。"""
    record = get_override(db, flag_key, user_id)
    if record is None:
        return False
    db.delete(record)
    db.commit()
    return True


def override_to_response(record: UserOverride) -> dict:
    return {
        "flag_key": record.flag_key,
        "user_id": record.user_id,
        "variant_key": record.variant_key,
        "reason": record.reason,
        "expires_at": record.expires_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def version_to_response(ver: FlagConfigVersion) -> dict:
    rules = [RuleIn(**item) for item in json.loads(ver.rules_json)]
    variants = [VariantIn(**item) for item in json.loads(ver.variants_json)]
    return {
        "flag_key": ver.flag_key,
        "version": ver.version,
        "description": ver.description,
        "variants": variants,
        "rules": rules,
        "created_at": ver.created_at,
    }


def save_snapshot(
    db: Session,
    *,
    request_id: str,
    flag_key: str,
    config_version: int,
    user_id: str,
    context: dict,
    decision: eng.Decision,
) -> DecisionSnapshot:
    snapshot = DecisionSnapshot(
        request_id=request_id,
        flag_key=flag_key,
        user_id=user_id,
        request_context_json=json.dumps(context, ensure_ascii=False, sort_keys=True),
        config_version=config_version,
        bucket_value=decision.bucket_value,
        matched_rule_index=decision.matched_rule_index,
        variant_key=decision.variant_key,
        decision_path=decision.decision_path,
        explanation_json=json.dumps(decision.explanation, ensure_ascii=False),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def get_snapshot(db: Session, request_id: str) -> DecisionSnapshot | None:
    return db.scalar(select(DecisionSnapshot).where(DecisionSnapshot.request_id == request_id))


def count_snapshots(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(DecisionSnapshot)) or 0


def max_snapshot_id(db: Session) -> int | None:
    """返回当前最大自增 id（首页发起时的高水位）；空表返回 None。"""
    return db.scalar(select(func.max(DecisionSnapshot.id)))


def ensure_snapshot_decision_path_column(db: Session) -> None:
    """为老库 decision_snapshots 幂等追加 decision_path 列。

    ``Base.metadata.create_all`` 只建新表、不会向老表追加新列；升级启动时
    通过 SQLAlchemy inspector 检测列是否存在，缺失才执行 ADD COLUMN
    （SQLite 3.35+ 与 PostgreSQL 均支持不带默认值的 ADD COLUMN，无需回填）。
    老行该列为 NULL，读取时按 matched_rule_index 回退推断。
    """
    from sqlalchemy import text

    columns = {c["name"] for c in inspect(db.bind).get_columns("decision_snapshots")}
    if "decision_path" not in columns:
        db.execute(text("ALTER TABLE decision_snapshots ADD COLUMN decision_path VARCHAR(16)"))
        db.commit()


def ensure_decision_audit_indexes(db: Session) -> None:
    """为已存在的 decision_snapshots 表幂等补齐审计列表索引。

    ``Base.metadata.create_all`` 只建新表、不会向老表追加新索引，
    因此升级启动时显式执行 CREATE INDEX IF NOT EXISTS（SQLite 与
    PostgreSQL 9.5+ 均支持），缺失才创建，已存在则为空操作。
    """
    from sqlalchemy import text

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_decided_at_request_id "
        "ON decision_snapshots (decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_flag_decided "
        "ON decision_snapshots (flag_key, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_user_decided "
        "ON decision_snapshots (user_id, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_variant_decided "
        "ON decision_snapshots (variant_key, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_flag_user_decided "
        "ON decision_snapshots (flag_key, user_id, decided_at, request_id)",
    )
    for ddl in statements:
        db.execute(text(ddl))
    db.commit()


def list_snapshots(
    db: Session,
    *,
    flag_key: str | None = None,
    user_id: str | None = None,
    variant_key: str | None = None,
    decided_from: datetime | None = None,
    decided_to: datetime | None = None,
    limit: int = 20,
    # 键集边界：严格位于 (decided_at < boundary_ts) 或
    # (decided_at == boundary_ts AND request_id < boundary_request_id)
    boundary_ts: datetime | None = None,
    boundary_request_id: str | None = None,
    # 上界高水位（首页请求发起时全表最大自增 id）：翻页期间新写入
    # 快照的 id 更大，一律被 id <= max_id 排除，保证翻页窗口不漂移、不重不漏。
    max_id: int | None = None,
) -> Sequence[DecisionSnapshot]:
    """按 (decided_at DESC, request_id DESC) 键集分页查询判定快照。

    多取 1 行（limit + 1）用于判断是否还有下一页；同一 decided_at 用
    request_id 做唯一并列裁决，保证同一时间戳不重不漏。
    """
    stmt = select(DecisionSnapshot)

    if flag_key is not None:
        stmt = stmt.where(DecisionSnapshot.flag_key == flag_key)
    if user_id is not None:
        stmt = stmt.where(DecisionSnapshot.user_id == user_id)
    if variant_key is not None:
        stmt = stmt.where(DecisionSnapshot.variant_key == variant_key)
    if decided_from is not None:
        stmt = stmt.where(DecisionSnapshot.decided_at >= decided_from)
    if decided_to is not None:
        stmt = stmt.where(DecisionSnapshot.decided_at <= decided_to)

    if boundary_ts is not None:
        # 必须携带成对的 request_id（游标保证）
        stmt = stmt.where(
            (DecisionSnapshot.decided_at < boundary_ts)
            | (
                (DecisionSnapshot.decided_at == boundary_ts)
                & (DecisionSnapshot.request_id < boundary_request_id)
            )
        )

    if max_id is not None:
        stmt = stmt.where(DecisionSnapshot.id <= max_id)

    stmt = stmt.order_by(
        DecisionSnapshot.decided_at.desc(), DecisionSnapshot.request_id.desc()
    ).limit(limit + 1)
    return db.scalars(stmt).all()
