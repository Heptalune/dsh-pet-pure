# -*- coding: utf-8 -*-
"""`pet/features.py` 的静态契约体检。

本文件只管**数据与纯函数**，不起 Qt、不读配置、不碰磁盘。三块：

1. 清单自洽（抄漏/写错 id 的第一道门）：id 唯一、依赖边指向真实模块、
   硬依赖无环、菜单动作与配置键不为空、分组/批次在登记范围内；
2. `normalize_enabled`：合法数组、保序去重、未知 id 分流、损坏形状抛错；
3. `resolve_dependencies`：硬依赖未满足进 blocked、闭包传递、可选增强不影响
   active、未知 id 忽略。

另有一条「零 Qt」的进程级检查：`pet/features.py` 要在任何可选服务创建之前
被 import，所以它连间接拉进 Qt 都不允许——用子进程验证，因为本进程早就被
其它测试 import 过 Qt 了。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pet import features
from pet.features import FEATURES, MIGRATION_BASELINE, FeatureSpec

#: SPEC §3 里可停用的模块全集（核心能力一律不在内）。
EXPECTED_IDS = {
    "weather",
    "divination",
    "todo",
    "festival",
    "voice",
    "music",
    "chat",
    "file_interpret",
    "vision",
    "proactive",
    "agent_link",
    "dynamic_island",
    "quick_launch",
    "system_tools",
    "stream_capture",
    "cross_screen",
}

REPO_ROOT = Path(__file__).resolve().parents[1]


def _chain_spec(feature_id: str, requires: tuple[str, ...] = ()) -> FeatureSpec:
    """只为传递闭包用例造的合成模块（不进真实清单）。"""
    return FeatureSpec(
        id=feature_id,
        name=feature_id,
        desc="合成用例",
        category=features.CATEGORY_SYSTEM,
        search_aliases=(feature_id,),
        requires=requires,
        optional_deps=(),
        menu_action_ids=(feature_id,),
        config_keys=(feature_id,),
        needs_network=False,
        needs_paid=False,
        needs_external_process=False,
        batch=features.BATCH_TOOLS,
    )


# ---------------------------------------------------------------- 清单自洽
class TestInventory:
    def test_exact_module_set(self):
        assert set(FEATURES) == EXPECTED_IDS

    def test_keys_match_spec_ids(self):
        for key, spec in FEATURES.items():
            assert spec.id == key

    def test_dependency_edges_point_to_real_modules(self):
        for spec in FEATURES.values():
            for dep in spec.requires + spec.optional_deps:
                assert dep in FEATURES, f"{spec.id} 依赖了不存在的模块 {dep}"

    def test_no_self_dependency(self):
        for spec in FEATURES.values():
            assert spec.id not in spec.requires
            assert spec.id not in spec.optional_deps

    def test_hard_dependencies_are_acyclic(self):
        # Kahn 拓扑排序：解不出来就是有环（环会让激活层永远等不到可运行集合）。
        indegree = {fid: 0 for fid in FEATURES}
        for spec in FEATURES.values():
            for dep in spec.requires:
                indegree[spec.id] += 1
                assert dep in FEATURES
        ready = [fid for fid, deg in indegree.items() if deg == 0]
        resolved = 0
        while ready:
            current = ready.pop()
            resolved += 1
            for spec in FEATURES.values():
                if current in spec.requires:
                    indegree[spec.id] -= 1
                    if indegree[spec.id] == 0:
                        ready.append(spec.id)
        assert resolved == len(FEATURES)

    def test_menu_action_ids_not_empty_unless_documented(self):
        # 每个模块至少一条菜单动作，防数据抄漏；唯一例外是清单里登记过的
        # 「没有菜单动作」的模块（文件解读只靠拖文件触发，见 MENU_LESS_FEATURES）。
        for spec in FEATURES.values():
            if spec.id in features.MENU_LESS_FEATURES:
                assert spec.menu_action_ids == ()
            else:
                assert spec.menu_action_ids, f"{spec.id} 没登记菜单动作"

    def test_menu_less_whitelist_only_names_real_modules(self):
        assert features.MENU_LESS_FEATURES <= set(FEATURES)

    def test_config_keys_not_empty(self):
        for spec in FEATURES.values():
            assert spec.config_keys, f"{spec.id} 没登记配置键"

    def test_text_fields(self):
        for spec in FEATURES.values():
            assert spec.name
            assert spec.desc
            assert "\n" not in spec.desc and "\r" not in spec.desc
            assert spec.search_aliases, f"{spec.id} 没登记搜索别名"
            assert spec.category in features.CATEGORY_ORDER
            assert spec.batch in {
                features.BATCH_FIRST,
                features.BATCH_SECOND,
                features.BATCH_THIRD,
                features.BATCH_TOOLS,
            }

    def test_batches_follow_spec_order(self):
        declared = {
            1: {"weather", "divination", "todo", "festival"},
            2: {"voice", "music"},
            3: {"chat", "file_interpret", "vision", "proactive", "agent_link", "dynamic_island"},
            4: {"quick_launch", "system_tools", "stream_capture", "cross_screen"},
        }
        for batch, ids in declared.items():
            assert {fid for fid, spec in FEATURES.items() if spec.batch == batch} == ids

    def test_dependency_edges_match_spec(self):
        # SPEC §3 的依赖边（硬/可选分开钉住，改动必须显式改这里）。
        assert FEATURES["file_interpret"].requires == ("chat",)
        assert FEATURES["proactive"].requires == ("chat",)
        assert FEATURES["vision"].requires == ("chat",)
        assert FEATURES["chat"].requires == ()
        for fid in ("divination", "todo", "festival"):
            assert FEATURES[fid].requires == ()
            assert FEATURES[fid].optional_deps == ("voice",)
        assert FEATURES["dynamic_island"].optional_deps == ("chat", "agent_link")


class TestQuerySurface:
    def test_all_features_sorted_by_batch_and_category(self):
        specs = features.all_features()
        assert len(specs) == len(FEATURES)
        groups = [
            (spec.batch, features.CATEGORY_ORDER.index(spec.category)) for spec in specs
        ]
        assert groups == sorted(groups)

    def test_all_features_keeps_declaration_order_inside_group(self):
        # 组内不按 id 重排：声明顺序 = SPEC §3 的实施顺序。
        got = [spec.id for spec in features.all_features()]
        declared = list(FEATURES)
        for batch in {spec.batch for spec in FEATURES.values()}:
            for category in features.CATEGORY_ORDER:
                group = [
                    fid
                    for fid in declared
                    if FEATURES[fid].batch == batch and FEATURES[fid].category == category
                ]
                assert [fid for fid in got if fid in group] == group

    def test_all_features_is_stable(self):
        assert features.all_features() == features.all_features()

    def test_get(self):
        assert features.get("weather") is FEATURES["weather"]
        assert features.get("nope") is None

    def test_migration_baseline_is_all_ids_in_order(self):
        # 注意：这张表刻意写死（老用户升级继承历史全集，新模块默认关）。
        # 以后新增模块时本断言会红——那时必须显式决定它不进基线，并把这里
        # 改成 `set(MIGRATION_BASELINE) <= set(FEATURES)`。
        assert MIGRATION_BASELINE == tuple(spec.id for spec in features.all_features())
        assert len(set(MIGRATION_BASELINE)) == len(MIGRATION_BASELINE)
        assert set(MIGRATION_BASELINE) == set(FEATURES)


# ---------------------------------------------------------------- 启用集合归一化
class TestNormalizeEnabled:
    def test_legal_list_splits_known_and_unknown(self):
        enabled, unknown = features.normalize_enabled(["weather", "nope", "todo"])
        assert enabled == ["weather", "todo"]
        assert unknown == ["nope"]

    def test_dedup_keeps_first_position(self):
        enabled, unknown = features.normalize_enabled(
            ["todo", "weather", "todo", "nope", "nope"]
        )
        assert enabled == ["todo", "weather"]
        assert unknown == ["nope"]

    def test_empty_list_is_legal_and_stays_empty(self):
        # SPEC §4：合法空列表 = 用户明确选择纯核心，绝不回退成全部启用。
        assert features.normalize_enabled([]) == ([], [])

    def test_all_known(self):
        enabled, unknown = features.normalize_enabled(list(EXPECTED_IDS))
        assert enabled == list(EXPECTED_IDS)
        assert unknown == []

    @pytest.mark.parametrize(
        "broken",
        [
            {"enabled": ["weather"]},
            "weather",
            "",
            3,
            0,
            1.5,
            True,
            None,
            [["weather"]],
            ["weather", 3],
            ["weather", None],
            [None],
        ],
    )
    def test_corrupt_shape_raises(self, broken):
        # 损坏不冒充新装：抛错让调用方保留原数据 + 给诊断，绝不返回默认集。
        with pytest.raises(ValueError):
            features.normalize_enabled(broken)

    def test_unknown_only(self):
        assert features.normalize_enabled(["nope", "also-nope"]) == ([], ["nope", "also-nope"])


# ---------------------------------------------------------------- 依赖解算
class TestResolveDependencies:
    def test_chat_alone_is_active(self):
        resolved = features.resolve_dependencies(["chat"])
        assert resolved.active == frozenset({"chat"})
        assert resolved.blocked == {}

    def test_file_interpret_alone_is_blocked_on_chat(self):
        resolved = features.resolve_dependencies(["file_interpret"])
        assert resolved.active == frozenset()
        assert resolved.blocked == {"file_interpret": ("chat",)}

    def test_file_interpret_with_chat_is_active(self):
        resolved = features.resolve_dependencies(["file_interpret", "chat"])
        assert resolved.active == frozenset({"file_interpret", "chat"})
        assert resolved.blocked == {}

    def test_transitive_closure_blocks_every_layer(self, monkeypatch):
        # 合成一条 A→B→C 的链：闭包要传递，每一层各自报「我缺谁」。
        chain = {
            "chain_a": _chain_spec("chain_a", requires=("chain_b",)),
            "chain_b": _chain_spec("chain_b", requires=("chain_c",)),
            "chain_c": _chain_spec("chain_c"),
        }
        monkeypatch.setattr(features, "FEATURES", {**FEATURES, **chain})
        resolved = features.resolve_dependencies(["chain_a"])
        assert resolved.active == frozenset()
        assert resolved.blocked == {"chain_a": ("chain_b",), "chain_b": ("chain_c",)}

    def test_optional_deps_do_not_block(self):
        # 可选增强缺了只是少一块能力：抽签/待办/节日没有语音照样激活。
        for fid in ("divination", "todo", "festival", "dynamic_island"):
            resolved = features.resolve_dependencies([fid])
            assert resolved.active == frozenset({fid}), fid
            assert resolved.blocked == {}, fid

    def test_optional_deps_do_not_change_active_set(self):
        without = features.resolve_dependencies(["dynamic_island"])
        with_deps = features.resolve_dependencies(["dynamic_island", "chat", "agent_link"])
        assert without.active <= with_deps.active

    def test_every_hard_edge_blocks_when_opened_alone(self):
        for spec in FEATURES.values():
            if not spec.requires:
                continue
            resolved = features.resolve_dependencies([spec.id])
            assert resolved.active == frozenset()
            assert resolved.blocked[spec.id] == spec.requires

    def test_hard_edge_satisfied_becomes_active(self):
        for spec in FEATURES.values():
            if not spec.requires:
                continue
            resolved = features.resolve_dependencies([spec.id, *spec.requires])
            assert spec.id in resolved.active
            assert resolved.blocked == {}

    def test_empty_input(self):
        resolved = features.resolve_dependencies([])
        assert resolved.active == frozenset()
        assert resolved.blocked == {}

    def test_unknown_ids_are_ignored(self):
        # 未知 id 的保留/诊断归 normalize_enabled，这里不参与解算。
        resolved = features.resolve_dependencies(["weather", "nope", 3])
        assert resolved.active == frozenset({"weather"})
        assert resolved.blocked == {}

    def test_all_enabled_is_fully_active(self):
        resolved = features.resolve_dependencies(EXPECTED_IDS)
        assert resolved.active == frozenset(EXPECTED_IDS)
        assert resolved.blocked == {}

    def test_accepts_any_iterable(self):
        assert features.resolve_dependencies(iter(["chat"])) == features.resolve_dependencies(
            ["chat"]
        )


# ---------------------------------------------------------------- 零 Qt 与轻量
def test_import_does_not_pull_in_qt():
    """启动早期就要 import：这个模块连间接拉进 Qt 都不允许。"""
    code = (
        "import sys, pet.features;"
        "bad=[m for m in sys.modules if m.split('.')[0] in {'PySide6','PyQt5','PyQt6','shiboken6'}];"
        "print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
