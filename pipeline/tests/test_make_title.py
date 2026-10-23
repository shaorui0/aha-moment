import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from extract_tasks import make_title  # noqa: E402


def test_commas_break_title():
    assert make_title("明天要给房东发邮件，确认一下合同") == "明天要给房东发邮件"
    assert make_title("call him, then pay") == "call him"


def test_number_comma_kept_and_length_cap():
    assert make_title("pay 1,000 yen today") == "pay 1,000 yen today"
    assert len(make_title("字" * 80)) <= 40
