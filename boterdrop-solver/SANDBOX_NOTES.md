# Sandbox / fresh-box notes for the solver

## Symptom
`camoufox fetch` (also triggered by `api_server.py` auto-install) fails with:

    Error: 403 Client Error: rate limit exceeded for url:
    https://api.github.com/repos/daijro/camoufox/releases

GitHub's API allows only 60 unauthenticated requests/hour per IP — shared
servers/CI burn through this fast.

## Manual install workaround (no API needed)

1. Find the latest release tag on the releases PAGE (not the API):
   https://github.com/daijro/camoufox/releases
   e.g. `v152.0.4-beta.30` → asset `camoufox-152.0.4-beta.30-lin.x86_64.zip`

2. Download directly (release asset downloads are not API-rate-limited):

   ```bash
   mkdir -p ~/.cache/camoufox/browsers/official
   cd ~/.cache/camoufox/browsers/official
   curl -L -o camoufox.zip \
     https://github.com/daijro/camoufox/releases/download/v152.0.4-beta.30/camoufox-152.0.4-beta.30-lin.x86_64.zip
   mkdir -p 152.0.4-beta.30 && cd 152.0.4-beta.30 && unzip -o ../camoufox.zip
   chmod -R 755 .
   ```

3. Register the version:

   ```bash
   cat > ~/.cache/camoufox/browsers/official/152.0.4-beta.30/version.json <<'EOF'
   {"version":"152.0.4","build":"beta.30","prerelease":true,"sha256":null,"created_at":null}
   EOF
   touch ~/.cache/camoufox/.0.5_FLAG
   cat > ~/.cache/camoufox/config.json <<'EOF'
   {"active_version": "browsers/official/152.0.4-beta.30"}
   EOF
   ```

   NOTE: create `.0.5_FLAG` BEFORE extracting — `camoufox_path()` deletes the
   whole cache dir when the flag is missing (old-layout cleanup).

4. Verify:

   ```bash
   python - <<'EOF'
   from camoufox.pkgman import launch_path
   print(launch_path())
   EOF
   ```

## Also patched in this copy
`api_server.py` now works on modern FastAPI (>=0.103): `add_event_handler`
was removed there, so the startup/shutdown hooks are wrapped in a lifespan
context with a fallback to the legacy API.
