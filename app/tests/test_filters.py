from app import filters


def test_strip_html_handles_entity_escaped_markup():
    # Greenhouse's `content` field arrives entity-escaped, sometimes twice (&amp;nbsp;)
    escaped = "&lt;p&gt;We use &lt;b&gt;React&lt;/b&gt;&amp;nbsp;and GitLab&#39;s values.&lt;/p&gt;"
    text = filters.strip_html(escaped)
    assert "&lt;" not in text and "<" not in text and "&" not in text
    assert "React" in text and "GitLab's values" in text
