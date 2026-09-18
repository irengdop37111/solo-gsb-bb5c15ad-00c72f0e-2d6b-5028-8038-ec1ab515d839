"""按用户临时强制变体（override）API 测试。"""

from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import UserOverride


OVERRIDE_PATH = "/api/v1/flags/btn_color/overrides/ov-user"


def _override_body(**overrides):
    body = {"variant_key": "blue", "reason": "客服补偿，强制蓝按钮"}
    body.update(overrides)
    return body


def _put(client, user_id="ov-user", flag_key="btn_color", **body_overrides):
    payload = body_overrides.pop("payload", None) or _override_body(**body_overrides)
    return client.put(f"/api/v1/flags/{flag_key}/overrides/{user_id}", json=payload)


def _evaluate(client, user_id="ov-user", **extra):
    body = {"user_id": user_id, "context": {"region": "cn"}}
    body.update(extra)
    return client.post("/api/v1/flags/btn_color/evaluate", json=body)


# ---------------------------------------------------------------------------
# PUT 写入 / 替换
# ---------------------------------------------------------------------------


def test_put_override_creates_record(client, simple_flag):
    resp = _put(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "flag_key": "btn_color",
        "user_id": "ov-user",
        "variant_key": "blue",
        "reason": "客服补偿，强制蓝按钮",
        "expires_at": None,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert body["created_at"] and body["updated_at"]


def test_put_override_unknown_flag_404(client):
    resp = client.put(
        "/api/v1/flags/nope/overrides/u1", json=_override_body()
    )
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_put_override_unknown_variant_404(client, simple_flag):
    resp = _put(client, variant_key="ghost")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
    # 未落库
    db = SessionLocal()
    try:
        assert db.query(UserOverride).count() == 0
    finally:
        db.close()


def test_put_override_past_expires_at_returns_422(client, simple_flag):
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    resp = _put(client, expires_at=past)
    assert resp.status_code == 422
    assert "expires_at" in resp.text


def test_put_override_naive_past_expires_at_treated_as_utc_422(client, simple_flag):
    # naive 时间按 UTC 解释：一个肯定早于现在的时刻
    resp = _put(client, expires_at="2020-01-01T00:00:00")
    assert resp.status_code == 422


def test_put_override_future_with_offset_accepted_and_normalized(client, simple_flag):
    # 未来时间 +08:00 偏移合法；响应按 UTC 表示
    future = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(
        timezone(timedelta(hours=8))
    )
    resp = _put(client, expires_at=future.isoformat())
    assert resp.status_code == 200, resp.text
    expires = datetime.fromisoformat(resp.json()["expires_at"])
    assert expires.tzinfo is None  # 存储与返回均为 naive UTC
    expected = future.astimezone(timezone.utc).replace(tzinfo=None)
    assert abs((expires - expected).total_seconds()) < 1


def test_put_override_rejects_empty_reason_and_variant(client, simple_flag):
    assert _put(client, payload={"variant_key": "", "reason": "x"}).status_code == 422
    assert _put(client, payload={"variant_key": "blue", "reason": ""}).status_code == 422


def test_put_override_replaces_existing_record(client, simple_flag):
    assert _put(client, variant_key="blue", reason="第一次").status_code == 200
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    resp = _put(client, variant_key="red", reason="第二次", expires_at=future)
    assert resp.status_code == 200
    body = resp.json()
    assert body["variant_key"] == "red"
    assert body["reason"] == "第二次"
    assert body["expires_at"] is not None

    db = SessionLocal()
    try:
        assert db.query(UserOverride).count() == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 判定短路
# ---------------------------------------------------------------------------


def test_override_short_circuits_rule_and_bucket(client, simple_flag):
    # simple_flag: region=cn 规则本应命中 red；override 强制 blue
    assert _put(client, variant_key="blue", reason="白名单").status_code == 200
    resp = _evaluate(client, request_id="ov-req-1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant_key"] == "blue"
    assert body["decision_path"] == "override"
    assert body["bucket_value"] is None
    assert body["matched_rule"] is None
    steps = [(e["step"], e["detail"]) for e in body["explanation"]]
    assert [s for s, _ in steps] == ["override_loaded", "override_selected"]
    loaded = dict(steps)["override_loaded"]
    assert loaded["variant_key"] == "blue"
    assert loaded["reason"] == "白名单"
    assert loaded["expires_at"] is None


def test_override_variant_zero_weight_still_forced(client, simple_flag):
    # 更新为 red=100/blue=0，再对 blue 下 override：分桶永不选中 blue，但 override 仍强制生效
    update = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "red", "weight": 100},
            {"key": "blue", "weight": 0},
        ],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=update).status_code == 201
    assert _put(client, variant_key="blue", reason="定向验证").status_code == 200
    body = _evaluate(client, context={}, request_id="ov-zero").json()
    assert body["variant_key"] == "blue"
    assert body["decision_path"] == "override"
    assert body["config_version"] == 2


def test_override_ignored_when_config_version_pinned(client, simple_flag):
    # 指定 config_version 时 override 不参与（钉版本回放语义）
    assert _put(client, variant_key="blue", reason="不应生效").status_code == 200
    body = _evaluate(
        client, request_id="ov-pinned", config_version=1
    ).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_expired_override_does_not_apply(client, simple_flag):
    assert _put(client, variant_key="blue", reason="临时").status_code == 200
    # 直接把库里的到期时间改成过去（绕过 PUT 的未来时间校验）
    db = SessionLocal()
    try:
        record = db.query(UserOverride).one()
        record.expires_at = datetime(2020, 1, 1)
        db.commit()
    finally:
        db.close()

    body = _evaluate(client, request_id="ov-expired").json()
    assert body["variant_key"] == "red"  # 回到规则命中
    assert body["decision_path"] == "rule"
    steps = [e["step"] for e in body["explanation"]]
    assert "override_loaded" not in steps


def test_override_removed_when_variant_removed_from_config(client, simple_flag):
    assert _put(client, variant_key="blue", reason="临时").status_code == 200
    # 更新配置：blue 被 green 替换（blue 不再存在）
    update = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "green", "weight": 50},
        ],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=update).status_code == 201
    body = _evaluate(client, context={}, request_id="ov-stale").json()
    assert body["variant_key"] in ("red", "green")
    assert body["decision_path"] == "bucket"

    # 记录仍保留在表中（未删除），只是不生效
    db = SessionLocal()
    try:
        record = db.query(UserOverride).one()
        assert record.variant_key == "blue"
    finally:
        db.close()


def test_override_becomes_effective_again_when_variant_readded(client, simple_flag):
    assert _put(client, variant_key="blue", reason="临时").status_code == 200
    update = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "green", "weight": 50},
        ],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=update).status_code == 201
    # blue 重新回到当前配置（版本 3），旧记录重新生效
    update["variants"] = [
        {"key": "red", "weight": 50},
        {"key": "blue", "weight": 50},
    ]
    assert client.post("/api/v1/flags", json=update).status_code == 201
    body = _evaluate(client, context={}, request_id="ov-revived").json()
    assert body["variant_key"] == "blue"
    assert body["decision_path"] == "override"


def test_override_with_future_expiry_applies(client, simple_flag):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert _put(client, variant_key="blue", reason="一小时有效", expires_at=future).status_code == 200
    body = _evaluate(client, request_id="ov-future").json()
    assert body["decision_path"] == "override"
    loaded = next(e for e in body["explanation"] if e["step"] == "override_loaded")
    assert loaded["detail"]["expires_at"] is not None


# ---------------------------------------------------------------------------
# 快照固化与幂等重放
# ---------------------------------------------------------------------------


def test_override_snapshot_persists_path_variant_reason(client, simple_flag):
    assert _put(client, variant_key="blue", reason="快照原因").status_code == 200
    body = _evaluate(client, request_id="ov-snap-1").json()
    assert body["decision_path"] == "override"

    snap = client.get("/api/v1/decisions/ov-snap-1").json()
    assert snap["variant_key"] == "blue"
    assert snap["decision_path"] == "override"
    assert snap["matched_rule_index"] is None
    assert snap["bucket_value"] is None
    loaded = next(e for e in snap["explanation"] if e["step"] == "override_loaded")
    assert loaded["detail"]["reason"] == "快照原因"

    # 审计列表同样带 decision_path 且可被 variant_key 过滤到
    listing = client.get(
        "/api/v1/decisions",
        params={"flag_key": "btn_color", "user_id": "ov-user", "variant_key": "blue"},
    ).json()
    assert any(i["request_id"] == "ov-snap-1" for i in listing["items"])
    assert listing["items"][0]["decision_path"] == "override"


def test_replay_keeps_first_result_after_override_changed(client, simple_flag):
    assert _put(client, variant_key="blue", reason="首次").status_code == 200
    first = _evaluate(client, request_id="ov-fixed-1").json()
    assert first["variant_key"] == "blue"

    # 修改 override：red 且原因变化
    assert _put(client, variant_key="red", reason="已改单").status_code == 200
    second = _evaluate(client, request_id="ov-fixed-1")
    assert second.json()["variant_key"] == "blue"
    assert second.json()["explanation"] == first["explanation"]
    assert second.json()["decision_path"] == "override"


def test_replay_keeps_first_result_after_override_expired(client, simple_flag):
    assert _put(client, variant_key="blue", reason="首次").status_code == 200
    first = _evaluate(client, request_id="ov-fixed-2").json()
    assert first["decision_path"] == "override"

    db = SessionLocal()
    try:
        record = db.query(UserOverride).one()
        record.expires_at = datetime(2020, 1, 1)
        db.commit()
    finally:
        db.close()

    # 已过期：新判定走规则，但旧 request_id 仍重放首次 override 结果
    fresh = _evaluate(client, request_id="ov-fresh-after-expiry").json()
    assert fresh["decision_path"] == "rule"
    replay = _evaluate(client, request_id="ov-fixed-2").json()
    assert replay["variant_key"] == "blue"
    assert replay["decision_path"] == "override"
    assert replay["explanation"] == first["explanation"]


def test_replay_keeps_first_result_after_override_deleted(client, simple_flag):
    assert _put(client, variant_key="blue", reason="首次").status_code == 200
    first = _evaluate(client, request_id="ov-fixed-3").json()

    resp = client.delete(OVERRIDE_PATH)
    assert resp.status_code == 204
    replay = _evaluate(client, request_id="ov-fixed-3").json()
    assert replay["variant_key"] == first["variant_key"] == "blue"
    assert replay["decision_path"] == "override"


def test_replay_keeps_first_result_after_variant_removed(client, simple_flag):
    assert _put(client, variant_key="blue", reason="首次").status_code == 200
    first = _evaluate(client, request_id="ov-fixed-4").json()
    assert first["variant_key"] == "blue"

    update = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "green", "weight": 50},
        ],
        "rules": [],
    }
    assert client.post("/api/v1/flags", json=update).status_code == 201
    replay = _evaluate(client, request_id="ov-fixed-4").json()
    assert replay["variant_key"] == "blue"
    assert replay["decision_path"] == "override"


# ---------------------------------------------------------------------------
# DELETE 撤销
# ---------------------------------------------------------------------------


def test_delete_existing_override_returns_204_and_restores_normal_decision(client, simple_flag):
    assert _put(client, variant_key="blue", reason="临时").status_code == 200
    assert _evaluate(client, request_id="ov-del-1").json()["decision_path"] == "override"

    resp = client.delete(OVERRIDE_PATH)
    assert resp.status_code == 204
    assert resp.content == b""

    body = _evaluate(client, request_id="ov-del-2").json()
    assert body["variant_key"] == "red"
    assert body["decision_path"] == "rule"


def test_delete_missing_override_is_idempotent_204(client, simple_flag):
    assert client.delete(OVERRIDE_PATH).status_code == 204
    assert client.delete(OVERRIDE_PATH).status_code == 204


def test_delete_override_unknown_flag_404(client):
    resp = client.delete("/api/v1/flags/nope/overrides/u1")
    assert resp.status_code == 404


def test_delete_flag_cascades_overrides(client, simple_flag):
    assert _put(client, variant_key="blue", reason="临时").status_code == 200
    assert client.delete("/api/v1/flags/btn_color").status_code == 204

    db = SessionLocal()
    try:
        assert db.query(UserOverride).filter(UserOverride.flag_key == "btn_color").count() == 0
    finally:
        db.close()

    # 同名重建后旧 override 不会复活：判定正常走规则
    payload = {
        "flag_key": "btn_color",
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "blue", "weight": 50},
        ],
        "rules": [
            {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "red"}
        ],
    }
    assert client.post("/api/v1/flags", json=payload).status_code == 201
    body = _evaluate(client, request_id="ov-after-recreate").json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_overrides_are_per_user_isolated(client, simple_flag):
    assert _put(client, user_id="forced-user", variant_key="blue", reason="仅该用户").status_code == 200
    forced = _evaluate(client, user_id="forced-user", request_id="ov-iso-1").json()
    other = _evaluate(client, user_id="normal-user", request_id="ov-iso-2").json()
    assert forced["decision_path"] == "override"
    assert forced["variant_key"] == "blue"
    assert other["decision_path"] == "rule"
    assert other["variant_key"] == "red"
