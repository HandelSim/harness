"""Verifier for harness/hello-harness."""

from pathlib import Path


def test_hello_file_exists():
    p = Path("/app/hello.txt")
    assert p.exists(), "/app/hello.txt was not created"


def test_hello_file_content():
    content = Path("/app/hello.txt").read_text().rstrip("\n")
    assert content == "hello-harness", f"unexpected content: {content!r}"
