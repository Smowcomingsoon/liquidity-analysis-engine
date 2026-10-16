"""Email allowlists (16/10, public repo): plain or sha256 entries."""

import hashlib

from app.config import Settings, email_allowlist


def test_plain_and_hashed_entries_match_case_insensitively():
    h = "sha256:" + hashlib.sha256(b"someone@example.com").hexdigest()
    allow = email_allowlist(f" Plain@Example.com ,{h}")
    assert "plain@example.com" in allow and "PLAIN@example.com" in allow
    assert "Someone@Example.com" in allow
    assert "other@example.com" not in allow and None not in allow
    assert allow and not email_allowlist("") and not email_allowlist(" , ")


def test_defaults_print_no_address():
    s = Settings(_env_file=None)
    for name in ("admin_emails", "full_access_emails", "telegram_allowed_emails",
                 "registration_allowed_emails", "balance_visible_emails"):
        v = getattr(s, name)
        assert "@" not in v and email_allowlist(v), name
