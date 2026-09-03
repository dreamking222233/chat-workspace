from app.services.response_content import SearchDirectiveStreamParser, normalize_assistant_content, strip_search_directives


def test_search_directive_is_hidden_across_real_gateway_chunk_boundaries():
    parser = SearchDirectiveStreamParser()
    visible: list[str] = []
    activities = []
    chunks = [
        'search("\\u4eca\\u5929\\u90fd\\u53d1\\u751f\\u4e86\\u4ec0\\u4e48")',
        'slow|2026年9月2日 今日新闻|1\n',
        '### 今日要闻\n\n- **市场**：保持关注。',
    ]
    for chunk in chunks:
        shown, found = parser.feed(chunk)
        visible.extend(shown)
        activities.extend(found)
    tail, found = parser.finish()
    visible.extend(tail)
    activities.extend(found)

    assert "".join(visible) == '### 今日要闻\n\n- **市场**：保持关注。'
    assert [item.query for item in activities] == ["2026年9月2日 今日新闻"]
    assert activities[0].original_query == "今天都发生了什么"
    assert activities[0].mode == "slow"
    assert activities[0].index == 1


def test_real_gateway_directive_has_identical_result_at_every_split_point():
    raw_variants = [
        'search("\\u4eca\\u5929\\u65b0\\u95fb")slow|latest news|1\r\n### answer',
        'search("\\u4eca\\u5929\\u65b0\\u95fb")slow|latest news\r\n### answer',
    ]
    for raw in raw_variants:
        for split in range(1, len(raw)):
            parser = SearchDirectiveStreamParser()
            first_visible, first_activities = parser.feed(raw[:split])
            second_visible, second_activities = parser.feed(raw[split:])
            tail, tail_activities = parser.finish()
            assert "".join([*first_visible, *second_visible, *tail]) == "### answer", (raw, split)
            activities = [*first_activities, *second_activities, *tail_activities]
            assert [item.query for item in activities] == ["latest news"], (raw, split)
            assert activities[0].index == 1


def test_complete_response_cleaner_handles_multiple_leading_searches():
    value = (
        'search("first")fast|first query|1\n'
        'search("second")slow|second query|2\n'
        '最终答案'
    )
    content, activities = strip_search_directives(value)
    assert content == "最终答案"
    assert [item.query for item in activities] == ["first query", "second query"]


def test_normal_or_malformed_search_text_is_preserved():
    normal = 'search("term") 是一个示例函数。\n后续正文'
    assert strip_search_directives(normal) == (normal, [])

    incomplete = 'search("term")slow|query'
    assert strip_search_directives(incomplete) == (incomplete, [])

    invalid_json = 'search("\\q")slow|query|1\n正文'
    assert strip_search_directives(invalid_json) == (invalid_json, [])

    empty_query = 'search("term")slow|   |1\n正文'
    assert strip_search_directives(empty_query) == (empty_query, [])

    invalid_mode = 'search("term")turbo|query|1\n正文'
    assert strip_search_directives(invalid_mode) == (invalid_mode, [])

    invalid_index = 'search("term")slow|query|0\n正文'
    assert strip_search_directives(invalid_index) == (invalid_index, [])

    oversized = 'search("' + ('x' * 8200) + '")slow|query|1\n正文'
    assert strip_search_directives(oversized) == (oversized, [])


def test_search_directive_without_trailing_newline_is_cleaned_on_finish():
    content, activities = strip_search_directives('search("today")slow|latest news|1')
    assert content == ""
    assert [item.query for item in activities] == ["latest news"]


def test_whole_response_markdown_fence_is_unwrapped_but_code_fence_is_preserved():
    wrapped = '```markdown\n# 标题\n\n- **项目**\n```'
    assert normalize_assistant_content(wrapped)[0] == '# 标题\n\n- **项目**'

    python_code = '```python\nprint("hello")\n```'
    assert normalize_assistant_content(python_code)[0] == python_code
