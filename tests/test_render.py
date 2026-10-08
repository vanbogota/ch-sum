from ghostwriter.control.render import parse_summary_args, split_text


def test_parse_summary_args():
    r = parse_summary_args("50 from @Vladimir 24h work stuff")
    assert (r.limit, r.author, r.since_hours, r.focus) == (50, "Vladimir", 24, "work stuff")
    r = parse_summary_args("от Володи 3д")
    assert (r.author, r.since_hours, r.limit) == ("Володи", 72, None)
    r = parse_summary_args(None)
    assert (r.limit, r.author, r.since_hours, r.focus) == (None, None, None, None)


def test_split_text():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(300))
    chunks = split_text(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
