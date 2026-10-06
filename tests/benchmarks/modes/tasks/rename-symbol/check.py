import os, re, subprocess, sys
for root, _, fs in os.walk("."):
    for f in fs:
        if f.endswith(".py") and re.search(r"\bget_usr\b", open(os.path.join(root, f)).read()):
            print("get_usr still present"); sys.exit(1)
r = subprocess.run([sys.executable, "run_tests.py"], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print("tests fail"); sys.exit(1)
print("ok")
