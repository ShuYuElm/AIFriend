"""用户档案：占位过滤、注入块、热重载。"""

from app.profile import ProfileStore, UserProfile, build_block, parse_profile


def test_parse_skips_placeholders():
    profile = parse_profile(
        {
            "name": "阿川",
            "facts": ["在上海", "王者荣耀在___区，ID 是___", "TODO 补充", "做开发工作"],
        }
    )
    assert profile.name == "阿川"
    assert profile.facts == ["在上海", "做开发工作"]


def test_parse_handles_empty():
    assert parse_profile(None).facts == []
    assert parse_profile({}).facts == []


def test_parse_sections_keep_order_and_dedupe():
    profile = parse_profile(
        {
            "name": "阿川",
            "sections": [
                {"title": "基本", "facts": ["在上海", "在上海"]},
                {"title": "游戏", "facts": ["常打对抗路"]},
            ],
        }
    )
    assert profile.facts == ["在上海", "常打对抗路"]


def test_parse_merges_sections_and_flat_facts():
    profile = parse_profile(
        {
            "sections": [{"title": "基本", "facts": ["在上海"]}],
            "facts": ["常打对抗路"],
        }
    )
    assert profile.facts == ["在上海", "常打对抗路"]


def test_parse_caps_facts():
    profile = parse_profile({"facts": [f"事实{i}" for i in range(60)]})
    assert len(profile.facts) == 40


def test_build_block_empty_when_no_facts():
    assert build_block(UserProfile()) == ""


def test_build_block_contains_header_and_facts():
    block = build_block(UserProfile(name="阿川", facts=["在上海"]))
    assert "关于他" in block
    assert "- 在上海" in block


def test_store_reloads_on_change(tmp_path):
    path = tmp_path / "user_profile.yaml"
    path.write_text("name: 阿川\nfacts:\n  - 在上海\n", encoding="utf-8")
    store = ProfileStore(path)
    assert store.load().facts == ["在上海"]

    path.write_text("name: 阿川\nfacts:\n  - 在上海\n  - 王者微信区\n", encoding="utf-8")
    # 部分文件系统 mtime 精度较低，显式推进一下
    import os
    import time

    os.utime(path, (time.time() + 1, time.time() + 1))
    assert store.load().facts == ["在上海", "王者微信区"]


def test_store_missing_file_is_safe(tmp_path):
    assert ProfileStore(tmp_path / "nope.yaml").load().facts == []
