# Installing harness with the bootstrap

1. Put these in **one folder** (the "bundle"):
   - `harness-bootstrap.sh`
   - `.env` with your values (optional)
   - `.harness-allowlist` (optional)

   The installer reads `.env` and `.harness-allowlist` from the folder
   `harness-bootstrap.sh` is in, **not** from the folder you run it from.
   The bundle folder must be writable.

2. `cd` to the folder where you want `harness/` created, then run:

   ```bash
   bash /path/to/bundle/harness-bootstrap.sh          # asks: main or dev
   bash /path/to/bundle/harness-bootstrap.sh -b main  # no question
   ```

   Use `source` instead of `bash` (in a bash shell) to get `harness` on your
   PATH right away; otherwise open a new shell.

3. Your `.env` and allowlist are copied into `harness/` (a `harness/.env`
   that already exists is left as is). Any `HTTP_PROXY`/`HTTPS_PROXY` in the
   bundled `.env` is also used for the download and clone.

4. Make sure `harness/.env` has `PROXY_API_URL`, `PROXY_API_KEY`, and
   `DEFAULT_MODEL_NAME`, then `cd` into a project and run `harness`.

Windows: run it from Git Bash, inside your user folder (`%USERPROFILE%`).
See [WINDOWS.md](WINDOWS.md).
