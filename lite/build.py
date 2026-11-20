#!/usr/bin/env python3
"""Build lite/harness-lite.ps1 (the one file users copy and run).

  gen_texts.py (opencode-texts.json + ../proxy)  ->  texts.ps1
  src.ps1 + texts.ps1 + ui.html                  ->  harness-lite.ps1

src.ps1 carries two placeholders, each exactly once: `#@@TEXTS@@` (replaced by
texts.ps1) and `#@@UI@@` (replaced by ui.html, inside a single-quoted
here-string). Output is ASCII with CRLF line ends and depends on the inputs
only (no timestamps), so tests/unit_lite_test.sh can compare bytes.

Usage: python3 lite/build.py [--out DIR]    (DIR defaults to lite/ itself)"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import gen_texts


def read(path):
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        return raw.decode('ascii').replace('\r\n', '\n')
    except UnicodeDecodeError as e:
        sys.exit(f'build: {path} is not ASCII (byte {e.start}); write non-ASCII as an escape or entity')


def build(src, texts, ui):
    for mark in ('#@@TEXTS@@', '#@@UI@@'):
        if src.count(mark) != 1:
            sys.exit(f'build: src.ps1 must contain {mark} exactly once, found {src.count(mark)}')
    if "$script:UiPage = @'\n#@@UI@@\n'@" not in src:
        sys.exit("build: #@@UI@@ must be the only line inside $script:UiPage = @' ... '@")
    for n, line in enumerate(ui.split('\n'), 1):
        if line.startswith("'@"):
            sys.exit(f"build: ui.html line {n} starts with '@ and would end the here-string")
    return src.replace('#@@TEXTS@@', texts.rstrip() + '\n').replace('#@@UI@@', ui.rstrip('\n'))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default=HERE, metavar='DIR', help='write texts.ps1 and harness-lite.ps1 here (default: lite/)')
    ap.add_argument('--proxy-dir', default=os.path.join(os.path.dirname(HERE), 'proxy'))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):   # the proxy prints banners on import
        texts = gen_texts.generate(os.path.abspath(a.proxy_dir))
    out = build(read(os.path.join(HERE, 'src.ps1')), texts, read(os.path.join(HERE, 'ui.html')))
    with open(os.path.join(a.out, 'texts.ps1'), 'w', encoding='ascii', newline='\n') as f:
        f.write(texts)
    with open(os.path.join(a.out, 'harness-lite.ps1'), 'w', encoding='ascii', newline='\r\n') as f:
        f.write(out)
    print(f"built {os.path.join(a.out, 'harness-lite.ps1')}: {out.count(chr(10))} lines")


if __name__ == '__main__':
    main()
