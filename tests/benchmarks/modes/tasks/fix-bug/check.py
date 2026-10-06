import hashlib, subprocess, sys
want = "823d1d156794af53c852aea032c25d66683fece49029f7c29f33812f3bcd675e"
got = hashlib.sha256(open("test_stats.py", "rb").read()).hexdigest()
if got != want:
    print("test_stats.py was modified"); sys.exit(1)
r = subprocess.run([sys.executable, "test_stats.py"], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print("tests still fail"); sys.exit(1)
print("ok")
