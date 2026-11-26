"""NormalizedEvent 单测：策略字段从原始事件正确派生。"""
from pismor.pipeline.normalize import NormalizedEvent, normalize


def test_conversation_splits_prompt_and_user_texts():
    ev = normalize({
        "event_type": "conversation",
        "messages": [
            {"role": "assistant", "text": "脚本语言选哪个？"},
            {"role": "user", "text": "以后都用 Python 3.12"},
            {"role": "assistant", "text": "好的"},
            {"role": "user", "text": "别再生成表格"},
        ],
        "force_extract": True,
    })
    assert isinstance(ev, NormalizedEvent)
    assert ev.event_type == "conversation"
    assert ev.mode == "dialogue"
    assert ev.is_dialogue
    # prompt 保留 assistant 上下文与顺序
    assert ev.prompt_text == (
        "assistant: 脚本语言选哪个？\n"
        "user: 以后都用 Python 3.12\n"
        "assistant: 好的\n"
        "user: 别再生成表格"
    )
    # 用户域文本只含 user 且保序
    assert ev.user_texts == ("以后都用 Python 3.12", "别再生成表格")
    assert ev.last_user_text == "别再生成表格"
    assert ev.force_extract is True


def test_conversation_filters_blank_user_text():
    ev = normalize({
        "event_type": "conversation",
        "messages": [
            {"role": "user", "text": "   "},
            {"role": "user", "text": "有效"},
        ],
    })
    assert ev.user_texts == ("有效",)
    assert ev.force_extract is False


def test_text_event_is_document():
    ev = normalize({"event_type": "text", "content": "周报正文"})
    assert ev.event_type == "text"
    assert ev.mode == "document"
    assert not ev.is_dialogue
    assert ev.prompt_text == "周报正文"
    assert ev.user_texts == ("周报正文",)
    assert ev.last_user_text == "周报正文"
    assert ev.force_extract is False


def test_missing_event_type_defaults_to_text():
    # intake 会回写 event_type；直调管线（测试/内部）时按 text 兜底，与 runner 旧行为一致
    ev = normalize({"content": "笔记"})
    assert ev.event_type == "text"
    assert ev.mode == "document"


def test_empty_content_has_no_user_text():
    ev = normalize({"event_type": "text", "content": ""})
    assert ev.user_texts == ()
    assert ev.last_user_text == ""


def test_force_extract_requires_exact_true():
    # pydantic lax 校验可能把 "False" 变字符串原样带进来；只有 True 才算强制
    assert normalize({"event_type": "conversation",
                      "messages": [{"role": "user", "text": "嗯"}],
                      "force_extract": "False"}).force_extract is False
    assert normalize({"event_type": "conversation",
                      "messages": [{"role": "user", "text": "嗯"}],
                      "force_extract": True}).force_extract is True
