import pytest

from xhs_cli.exceptions import AccessTooFrequentError, XhsApiError
from xhs_cli.html_parser import parse_initial_state


def test_parse_initial_state_raises_rate_limited_for_website_login_error_url():
    html = "<html><body>error page</body></html>"

    with pytest.raises(AccessTooFrequentError) as excinfo:
        parse_initial_state(
            html,
            final_url=(
                "https://www.xiaohongshu.com/website-login/error"
                "?error_code=300013&error_msg=%E8%AE%BF%E9%97%AE%E9%A2%91%E7%B9%81"
            ),
        )

    assert excinfo.value.code == "300013"


def test_parse_initial_state_preserves_generic_parse_error_for_non_limit_html():
    html = "<html><body>ordinary page</body></html>"

    with pytest.raises(XhsApiError, match="Could not parse __INITIAL_STATE__ from HTML"):
        parse_initial_state(html, final_url="https://www.xiaohongshu.com/explore/note-1")
