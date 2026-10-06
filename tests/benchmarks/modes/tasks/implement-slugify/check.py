import subprocess, sys
CASES = [("Hello, World!", "hello-world"), ("  Release v2.0 -- notes ", "release-v2-0-notes"),
         ("___", "n-a"), ("", "n-a"), ("Café au lait", "caf-au-lait"), ("A1-B2", "a1-b2"),
         ("--x--", "x"), ("MiXeD   Case\tTabs", "mixed-case-tabs")]
code = "import json,sys\nfrom text_utils import slugify\nprint(json.dumps([slugify(a) for a, _ in %r]))" % (CASES,)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print("slugify raised"); sys.exit(1)
import json
got = json.loads(r.stdout.strip().splitlines()[-1])
bad = [a for (a, w), g in zip(CASES, got) if g != w]
if bad:
    print("%d of %d cases wrong" % (len(bad), len(CASES))); sys.exit(1)
print("ok")
