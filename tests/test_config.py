"""config 模块测试：默认值 / 深合并 / 原子保存 / 损坏容忍 / env 覆盖 / 旧配置迁移。"""

import json

from autocondense.config import (PORT_ENV, load_config, resolve_data_dir,
                                 save_config, validate_config_dict)


def test_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.port == 8761
    assert cfg.host == "127.0.0.1"
    assert cfg.enabled is True
    assert cfg.cooldown_minutes == 30
    assert cfg.turns_threshold == 25
    assert cfg.water_threshold == 0.7
    assert cfg.usable_context_tokens == 160000
    assert cfg.task_min_rounds == 5
    assert cfg.judge_defer_turns == 5
    assert cfg.restore_max_chars == 8000
    assert cfg.restore_top_n == 3
    # v0.3.0 新增配置项
    assert cfg.restore_reread_files == 5
    assert cfg.delta_mode is True
    assert cfg.delta_merge_every == 5
    assert cfg.idle_signal_rounds == 3
    assert cfg.judge_defer_max_turns == 20
    assert cfg.sme.enabled is True
    assert cfg.sme.base_url == "http://127.0.0.1:8000"
    assert cfg.sme.timeout_s == 2.0
    assert cfg.sme.tags == ["project", "todo", "session_digest"]
    assert cfg.sme.importance == 0.7
    assert cfg.sme.split_segments is True
    assert cfg.sme.max_segments == 15
    assert cfg.sme.dedup is True
    assert cfg.sme.dedup_threshold == 0.92
    # 不再有 mode 字段
    assert not hasattr(cfg, "mode")


def test_deep_merge(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"cooldown_minutes": 10, "sme": {"timeout_s": 0.5}}),
        encoding="utf-8")
    cfg = load_config(tmp_path)
    # 显式给出的键生效
    assert cfg.cooldown_minutes == 10
    assert cfg.sme.timeout_s == 0.5
    # 未给出的键保持默认（深合并，嵌套对象不被整体替换）
    assert cfg.sme.base_url == "http://127.0.0.1:8000"
    assert cfg.sme.tags == ["project", "todo", "session_digest"]
    assert cfg.sme.split_segments is True
    assert cfg.turns_threshold == 25


def test_atomic_save_roundtrip(tmp_path):
    cfg = load_config(tmp_path)
    cfg.turns_threshold = 9
    cfg.sme.importance = 0.8
    save_config(tmp_path, cfg)
    reloaded = load_config(tmp_path)
    assert reloaded.turns_threshold == 9
    assert reloaded.sme.importance == 0.8
    # 原子写不留临时文件
    assert not list(tmp_path.glob("*.tmp-*"))


def test_corrupt_config_tolerated(tmp_path):
    (tmp_path / "config.json").write_text("{broken json", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.port == 8761  # 回落默认
    assert list(tmp_path.glob("config.json.corrupt-*"))  # 现场保留
    assert not (tmp_path / "config.json").exists()


def test_legacy_mode_key_migrated_not_crash(tmp_path):
    """旧配置文件含 mode 键：sanitize 丢弃并告警，绝不崩溃；enabled 取默认 True。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"mode": "manual", "turns_threshold": 12,
                    "sme": {"enabled": True}}),
        encoding="utf-8")
    cfg = load_config(tmp_path)
    assert not hasattr(cfg, "mode")
    assert cfg.enabled is True  # 旧配置无 enabled → 默认 True
    assert cfg.turns_threshold == 12  # 其余键正常生效


def test_legacy_auto_mode_also_migrated(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"mode": "auto"}), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert not hasattr(cfg, "mode") and cfg.enabled is True


def test_env_port_override(tmp_path, monkeypatch):
    monkeypatch.setenv(PORT_ENV, "9001")
    assert load_config(tmp_path).port == 9001


def test_env_port_invalid_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(PORT_ENV, "not-a-number")
    assert load_config(tmp_path).port == 8761


def test_resolve_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOCONDENSE_DATA_DIR", str(tmp_path / "dd"))
    assert resolve_data_dir() == (tmp_path / "dd").resolve()
    monkeypatch.delenv("AUTOCONDENSE_DATA_DIR")
    # 默认数据目录与代码位置解耦：用户级 ~/.autocondense（插件更新换缓存目录也不丢数据）
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("autocondense.config.Path.home", staticmethod(lambda: fake_home))
    assert resolve_data_dir() == fake_home / ".autocondense"


def test_validate_config_dict():
    assert validate_config_dict({"cooldown_minutes": 5}) == []
    assert validate_config_dict({"sme": {"timeout_s": 1.0}}) == []
    assert validate_config_dict({"sme": {"tags": ["a"]}}) == []
    # 新键校验
    assert validate_config_dict({"enabled": False}) == []
    assert validate_config_dict({"judge_defer_turns": 8}) == []
    assert validate_config_dict({"sme": {"split_segments": False}}) == []
    assert validate_config_dict({"sme": {"max_segments": 20}}) == []
    assert validate_config_dict({"restore_reread_files": 3}) == []
    assert validate_config_dict({"delta_mode": False}) == []
    assert validate_config_dict({"delta_merge_every": 8}) == []
    assert validate_config_dict({"idle_signal_rounds": 5}) == []
    assert validate_config_dict({"judge_defer_max_turns": 30}) == []
    assert validate_config_dict({"sme": {"dedup": False}}) == []
    assert validate_config_dict({"sme": {"dedup_threshold": 0.9}}) == []
    assert any("整数" in e for e in validate_config_dict({"restore_reread_files": "5"}))
    assert any("布尔" in e for e in validate_config_dict({"delta_mode": "off"}))
    assert any("整数" in e for e in validate_config_dict({"delta_merge_every": 1.5}))
    assert any("整数" in e for e in validate_config_dict({"idle_signal_rounds": True}))
    assert any("整数" in e for e in validate_config_dict({"judge_defer_max_turns": "max"}))
    assert any("布尔" in e for e in validate_config_dict({"sme": {"dedup": 1}}))
    assert any("数字" in e for e in validate_config_dict({"sme": {"dedup_threshold": "high"}}))
    assert any("布尔" in e for e in validate_config_dict({"enabled": "yes"}))
    assert any("整数" in e for e in validate_config_dict({"judge_defer_turns": "soon"}))
    assert any("布尔" in e for e in validate_config_dict({"sme": {"split_segments": 1}}))
    assert any("整数" in e for e in validate_config_dict({"sme": {"max_segments": "many"}}))
    # mode 已是未知键（老键会被拒绝，加载期才会被宽容丢弃）
    assert any("未知配置项" in e for e in validate_config_dict({"mode": "manual"}))
    assert any("未知配置项" in e for e in validate_config_dict({"foo": 1}))
    assert any("未知配置项" in e for e in validate_config_dict({"sme": {"bar": 1}}))
    assert any("整数" in e for e in validate_config_dict({"cooldown_minutes": "x"}))
    assert any("布尔" in e for e in validate_config_dict({"sme": {"enabled": "yes"}}))
    assert validate_config_dict(["not", "dict"]) != []
