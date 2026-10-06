import subprocess, sys
HEADER = "# owner: bench-7731"
missing = []
for f in ("primes.py", "test_primes.py", "NOTES.md"):
    try:
        first = open(f).readline().rstrip("\r\n")
    except OSError:
        print("%s missing" % f); sys.exit(1)
    if first != HEADER:
        missing.append(f)
if missing:
    print("header missing in: %s" % ",".join(missing)); sys.exit(1)
r = subprocess.run([sys.executable, "primes.py"], capture_output=True, text=True, timeout=60)
if r.stdout.strip() != "[2, 3, 5, 7, 11, 13, 17, 19, 23, 29]":
    print("primes.py output wrong"); sys.exit(1)
r = subprocess.run([sys.executable, "-c", "from primes import primes_below as p; assert p(2) == [] and p(3) == [2] and p(20) == [2,3,5,7,11,13,17,19]"],
                   capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print("primes_below wrong on edge cases"); sys.exit(1)
print("ok")
