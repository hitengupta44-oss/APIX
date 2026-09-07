# Windows notes

Most instructions in these docs assume a bash shell. Here is what changes on
Windows. If you use Git Bash or WSL, follow the bash versions instead.

## Setting environment variables

Never paste a multi-line `python -c "..."` block into a shell — quoting rules
differ between cmd.exe, PowerShell and bash, and you get errors like
`'from' is not recognized as an internal or external command`. Run a script
file instead; `python scripts/verify_setup.py` covers every local check.

**Command Prompt (cmd.exe)** — one variable per line, no quotes, no spaces
around `=`:

```
set SUPABASE_URL=https://xxxx.supabase.co
set SUPABASE_SERVICE_KEY=eyJhbGci...
set AGGREGATOR_API_TOKEN=duffel_test_...
```

**PowerShell:**

```powershell
$env:SUPABASE_URL="https://xxxx.supabase.co"
$env:SUPABASE_SERVICE_KEY="eyJhbGci..."
$env:AGGREGATOR_API_TOKEN="duffel_test_..."
```

Both last only for that window, which is what you want. Do not use
`setx` — it writes the value into your Windows user profile permanently, which
is the disk-resident secret you were trying to avoid.

## Command equivalents

| bash | Command Prompt | PowerShell |
|---|---|---|
| `export X=y` | `set X=y` | `$env:X="y"` |
| `python3` | `python` | `python` |
| `rm -rf dir` | `rmdir /s /q dir` | `Remove-Item -Recurse -Force dir` |
| `cp -r a b` | `xcopy /E /I a b` | `Copy-Item -Recurse a b` |
| `cat f` | `type f` | `Get-Content f` |
| `curl -s url` | `curl url` | `Invoke-RestMethod url` |

`python3` usually does not exist on Windows. Use `python`, and check with
`python --version` that it is 3.11 or later — if it opens the Microsoft Store,
install Python from python.org and tick **Add python.exe to PATH**.

## Generating the admin token

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Single line, double quotes. This one works in cmd.exe.

## Deploying to Hugging Face without rsync

`rsync` is not on Windows. Use PowerShell:

```powershell
git clone https://huggingface.co/spaces/<user>/apix hf-space
Copy-Item -Path .\apix\* -Destination .\hf-space\ -Recurse -Force `
  -Exclude @('web','node_modules','.git')
cd hf-space
Move-Item SPACE_README.md README.md -Force
git add -A
git commit -m "deploy apix api"
git push
```

Check that `Dockerfile` and `README.md` are both at the top level of
`hf-space` before pushing. If `Dockerfile` is missing, the Space build fails
immediately; if `README.md` lacks the YAML frontmatter, it builds and then
never serves.

## Line endings

If git rewrites line endings and the Docker build fails on a script, set:

```
git config --global core.autocrlf input
```

This matters because the container runs Linux, and a shell script with CRLF
endings fails there with a confusing "not found" error.
