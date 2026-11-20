<#
harness-lite: harness in one PowerShell file, for locked-down Windows machines.

It does what `harness host` does by default (opencode + the harness proxy),
without the proxy, opencode, docker, Python or any exe:

Every request is built the way host builds it. The system message is
opencode's: its prompt for the model family, the <env> block, every
instruction file (~/.claude/CLAUDE.md or ~/.config/opencode/AGENTS.md, plus
the project's AGENTS.md / CLAUDE.md / CONTEXT.md) and the skills list. The
tools are opencode's full set with its own descriptions: bash, read, edit,
write, glob, grep, todowrite, webfetch, websearch, skill, task (gpt-* models
get apply_patch in place of edit/write, as in opencode). The proxy then
shapes the request:

  *-withtools models   native tool calls, the slim prompt and slim tool
                       descriptions, a per-step reminder (todo list, step
                       count, repeated calls), old tool output trimmed,
                       max_tokens 32000
  every other model    the proxy's hybrid prompt mode: tools in the prompt
                       with tool-guidance.json, history translated with the
                       proxy's markers, reminder.md (rules, todo list,
                       environment, tool entries) on the last message,
                       require-tool with `finish`, malformed-call retry,
                       open-todo `finish` bounce and the empty-reply rescue

`task` runs an explore or general subagent with its own request shape, as
opencode does (on a native model with a free counterpart, explore goes to
the free model, and general is capped at 15 steps, as the proxy does). The
texts come byte for byte from opencode 1.19.3 and the harness proxy
(proxy.py, reminder.md, tool-guidance.json, agents/slim-*.md). `bash` runs
Git Bash when it is installed and allowed to run, else PowerShell.

Usage (from the project folder):
  $env:HARNESS_API_URL = 'https://your-upstream'   # same value as PROXY_API_URL
  .\harness-lite.ps1                  # interactive; lists models to pick from
  .\harness-lite.ps1 -Prompt "..."    # one task, then exit
  .\harness-lite.ps1 -Resume          # continue the last conversation in this folder
Key: $env:HARNESS_API_KEY if set, else asked for (hidden). Model: -Model or $env:HARNESS_MODEL.
Commands ask before they run unless -AutoApprove; /undo reverts file changes.
Optional: -Auth api-key for Azure-style keys, -Ascii if the console font lacks
the symbols, $env:HARNESS_LITE_SHELL = 'powershell' or a bash.exe path.
Debug: $env:HARNESS_LITE_DUMP = 'C:\some\folder' saves every request sent upstream
as a .json file there (compare with harness host's OUTPUT_DIR dumps).
#>
param(
    [string]$BaseUrl = $env:HARNESS_API_URL,
    [string]$Model = $env:HARNESS_MODEL,
    [string]$Auth = $env:HARNESS_AUTH,
    [string]$Root = '.',
    [string]$Prompt,
    [switch]$AutoApprove,
    [switch]$Resume,
    [switch]$Ascii,
    [switch]$Gui,
    [switch]$Tui,
    [int]$MaxSteps = 100,
    [int]$CommandTimeoutSec = 120
)

$ErrorActionPreference = 'Stop'
# Started by double-click the console closes with the script: show a fatal error first.
trap { if (-not $Prompt) { Write-Host "harness-lite stopped: $_" -ForegroundColor Red; $null = Read-Host 'Press Enter to close' }; break }
$script:Version = '4.0'
$script:DumpN = 0
$ProgressPreference = 'SilentlyContinue'   # 5.1's web progress bar is ugly and very slow
$script:IsPS5 = $PSVersionTable.PSVersion.Major -lt 6
$script:LangMode = [string]$ExecutionContext.SessionState.LanguageMode
$script:OnWindows = $env:OS -eq 'Windows_NT'
if ($script:IsPS5) { try { if ([int][Net.ServicePointManager]::SecurityProtocol -ne 0) { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } } catch { } }
$script:Sep = '/'
if ($script:OnWindows) { $script:Sep = '\' }
$script:MaxToolOutput = 50000
$script:HistoryCap = 350000   # chars of history kept; the upstream 502s somewhere past ~600k per request

# Windows PowerShell 5.1 serializes some arrays as {"value":[...],"Count":n}.
if ($script:IsPS5) { try { Remove-TypeData System.Array -ErrorAction SilentlyContinue } catch { } }

# ---------------------------------------------------------------- terminal UI
$script:Esc = [string][char]27
$script:VT = $false
try { $script:VT = [bool]$Host.UI.SupportsVirtualTerminal } catch { }
$script:Fancy = $Host.Name -eq 'ConsoleHost'   # `r redraw works; not in the ISE
if ($Ascii -or $env:HARNESS_LITE_ASCII) {
    $script:G = @{ dot = '*'; ring = 'o'; elbow = '`-'; bar = '|'; rule = '-'; prompt = '>'; bullet = '-'; mid = '|' }
}
else {
    # Only glyphs from the WGL4 set, which every Windows console font has.
    $script:G = @{ dot = [string][char]0x25CF; ring = [string][char]0x25CB; elbow = [string][char]0x2514
        bar = [string][char]0x2502; rule = [string][char]0x2500; prompt = [string][char]0x203A
        bullet = [string][char]0x2022; mid = [string][char]0x00B7 }
}
$script:WaitShown = $false
$script:WaitLen = 0

function Get-Width {
    $w = 0
    try { $w = [int]$Host.UI.RawUI.WindowSize.Width } catch { }
    if ($w -lt 40) { $w = 100 }
    return $w
}

function Clear-Wait {
    if (-not $script:WaitShown) { return }
    $script:WaitShown = $false
    Write-Host ("`r" + (' ' * $script:WaitLen) + "`r") -NoNewline
}

function Show-Wait([string]$label) {
    if (-not $script:Fancy) { return }
    Clear-Wait
    $s = Limit-Line ('  ' + $script:G.ring + ' ' + $label) ((Get-Width) - 2)
    Write-Host $s -NoNewline -ForegroundColor DarkGray
    $script:WaitLen = $s.Length
    $script:WaitShown = $true
}

function W([string]$t, [string]$c) {
    if ($t -eq '') { return }
    Clear-Wait
    if ($c) { Write-Host $t -NoNewline -ForegroundColor $c } else { Write-Host $t -NoNewline }
}

function WL([string]$t, [string]$c) {
    Clear-Wait
    if ($c) { Write-Host $t -ForegroundColor $c } else { Write-Host $t }
}

function Say([string]$text, [string]$color) { WL $text $color }

function Limit-Line([string]$s, [int]$n) {
    $s = $s.Replace("`t", '    ')
    if ($n -lt 8) { $n = 8 }
    if ($s.Length -le $n) { return $s }
    return $s.Substring(0, $n - 3) + '...'
}

function Show-Sub([string]$t, [string]$c = 'DarkGray') { WL (Limit-Line ('  ' + $script:G.elbow + ' ' + $t) ((Get-Width) - 1)) $c }
function Show-More([string]$t, [string]$c = 'DarkGray') { WL (Limit-Line ('    ' + $t) ((Get-Width) - 1)) $c }
function Get-Plural([int]$n, [string]$word) { if ($n -eq 1) { return "1 $word" }; return "$n ${word}s" }
function Show-Rule { WL ($script:G.rule * ((Get-Width) - 1)) 'DarkGray' }

function Get-Inline([string]$s, [string]$color) {
    # `code` and **bold** spans of one markdown line.
    $out = @()
    foreach ($p in ($s -split '(`[^`]+`|\*\*[^*]+\*\*)')) {
        if ($p -eq '') { continue }
        if ($p.Length -ge 2 -and $p.StartsWith('`') -and $p.EndsWith('`')) { $out += @{ t = $p.Substring(1, $p.Length - 2); c = 'Cyan'; b = $false } }
        elseif ($p.Length -ge 4 -and $p.StartsWith('**') -and $p.EndsWith('**')) { $out += @{ t = $p.Substring(2, $p.Length - 4); c = $color; b = $true } }
        else { $out += @{ t = $p; c = $color; b = $false } }
    }
    return $out
}

function Write-Wrapped($prefix, $segs, [string]$hang) {
    # Prefix pieces, then word-wrapped segments; continuation lines start with $hang.
    $width = (Get-Width) - 1
    $col = 0
    foreach ($p in $prefix) { W $p.t $p.c; $col += $p.t.Length }
    $start = $col
    $space = $false
    foreach ($seg in $segs) {
        $words = $seg.t.Replace("`t", '    ') -split ' '
        for ($k = 0; $k -lt $words.Count; $k++) {
            if ($k -gt 0) { $space = $true }
            $w = $words[$k]
            if ($w -eq '') { continue }
            $need = $w.Length
            if ($space) { $need++ }
            if ($col + $need -gt $width -and $col -gt $start) { WL ''; W $hang; $col = $hang.Length; $space = $false }
            if ($space) { W ' '; $col++ }
            $txt = $w
            if ($seg.b -and $script:VT) { $txt = $script:Esc + '[1m' + $w + $script:Esc + '[22m' }
            W $txt $seg.c
            $col += $w.Length
            $space = $false
        }
    }
    WL ''
}

function Write-Markdown([string]$text) {
    # The assistant's reply: light markdown (headings, lists, code fences, `code`, **bold**), wrapped.
    $lines = $text.Trim() -split "`r?`n"
    $first = $true
    $inCode = $false
    $prevBlank = $false
    foreach ($line in $lines) {
        $lead = @{ t = '  '; c = $null }
        if ($first) { $lead = @{ t = $script:G.dot + ' '; c = $null }; $first = $false }
        if ($line -match '^\s*```(.*)$') {
            $inCode = -not $inCode
            if ($inCode -and $Matches[1].Trim()) { W $lead.t $lead.c; WL ($script:G.bar + ' ' + $Matches[1].Trim()) 'DarkGray' }
            elseif ($lead.t.Trim()) { WL $lead.t }
            continue
        }
        if ($inCode) { W $lead.t $lead.c; W ($script:G.bar + ' ') 'DarkGray'; WL $line.Replace("`t", '    '); continue }
        if (-not $line.Trim()) {
            if (-not $prevBlank) { WL '' }
            $prevBlank = $true
            continue
        }
        $prevBlank = $false
        if ($line -match '^\s{0,3}#{1,6}\s+(.+)$') {
            Write-Wrapped @($lead) (Get-Inline ($Matches[1] -replace '\*\*', '') 'Cyan') '  '
        }
        elseif ($line -match '^(\s*)[-*+]\s+(.*)$') {
            $ind = ' ' * [int]($Matches[1].Length / 2) * 2
            $body = $Matches[2]
            Write-Wrapped @($lead, @{ t = $ind + $script:G.bullet + ' '; c = 'DarkGray' }) (Get-Inline $body $null) ('    ' + $ind)
        }
        elseif ($line -match '^(\s*)(\d+[.)])\s+(.*)$') {
            $ind = ' ' * [int]($Matches[1].Length / 2) * 2
            $num = $Matches[2]
            $body = $Matches[3]
            Write-Wrapped @($lead, @{ t = $ind + $num + ' '; c = 'DarkGray' }) (Get-Inline $body $null) ('  ' + $ind + (' ' * ($num.Length + 1)))
        }
        elseif ($line -match '^\s*>\s?(.*)$') {
            Write-Wrapped @($lead, @{ t = $script:G.bar + ' '; c = 'DarkGray' }) (Get-Inline $Matches[1] 'DarkGray') ('  ' + $script:G.bar + ' ')
        }
        elseif ($line -match '^\s*([-*_])(\s*\1){2,}\s*$') { W $lead.t $lead.c; WL ($script:G.rule * 20) 'DarkGray' }
        elseif ($line -match '^\s*\|') {
            $c = $null
            if ($line -match '^[\s|:-]+$') { $c = 'DarkGray' }
            W $lead.t $lead.c; WL $line.Trim() $c
        }
        else { Write-Wrapped @($lead) (Get-Inline $line.Trim() $null) '  ' }
    }
}

# ---------------------------------------------------------------- setup
# Config: harness-lite.json beside this file (api_url, api_key, model, auth).
# Per value: parameter, then the file, then environment, then asking (Confirm-Config).
$script:ConfigFile = $env:HARNESS_LITE_CONFIG
if (-not $script:ConfigFile) { $cfgDir = $PSScriptRoot; if (-not $cfgDir) { $cfgDir = (Get-Location).Path }; $script:ConfigFile = Join-Path $cfgDir 'harness-lite.json' }
$script:Cfg = @{ api_url = ''; api_key = ''; model = ''; auth = '' }
try {
    if (Test-Path -LiteralPath $script:ConfigFile) {
        $cfgIn = Get-Content -LiteralPath $script:ConfigFile -Raw | ConvertFrom-Json
        foreach ($k in @('api_url', 'api_key', 'model', 'auth')) { if ($cfgIn.$k) { $script:Cfg[$k] = '' + $cfgIn.$k } }
    }
}
catch { $script:CfgWarn = "cannot read $($script:ConfigFile) (it will be overwritten): $($_.Exception.Message)"; Write-Host "  $($script:CfgWarn)" }
# Saved (typed) values win over the environment; an explicit parameter wins over both.
$urlGiven = (@($PSBoundParameters.Keys) -contains 'BaseUrl')
$script:ModelGiven = [bool]$Model
if (-not (@($PSBoundParameters.Keys) -contains 'Model') -and $script:Cfg.model) { $Model = $script:Cfg.model; $script:ModelGiven = $false }
if (-not (@($PSBoundParameters.Keys) -contains 'Auth') -and $script:Cfg.auth) { $Auth = $script:Cfg.auth }
if (-not $Auth) { $Auth = 'bearer' }
if ($Auth -ne 'bearer' -and $Auth -ne 'api-key') { throw "-Auth must be 'bearer' or 'api-key'" }

function Set-ApiConfig([string]$url, [string]$key, [string]$authScheme) {
    $u = ('' + $url).Trim().TrimEnd('/')
    $script:BaseUrl = $u
    if ($u -match '/v1$') { $script:ApiRoot = $u } else { $script:ApiRoot = "$u/v1" }
    $script:ApiKey = ('' + $key).Trim()
    if ($authScheme -eq 'bearer' -or $authScheme -eq 'api-key') { $script:Auth = $authScheme }
}

function Save-Config([bool]$tell) {
    try { Set-Content -LiteralPath $script:ConfigFile -Value (ConvertTo-AsciiJson ($script:Cfg | ConvertTo-Json)) -Encoding ASCII; if ($tell) { Say "  saved to $($script:ConfigFile)" 'DarkGray' } }
    catch { Say "  cannot write $($script:ConfigFile): $($_.Exception.Message)" 'DarkYellow' }
}

function Read-Config([string]$req, [string]$err) {
    # Console prompts; Enter keeps the current value. $null = no more input.
    $label = 'API base URL (the PROXY_API_URL value)'
    if ($script:BaseUrl) { $label = "API base URL (Enter keeps $($script:BaseUrl))" }
    $u = Read-Host $label
    if ($null -eq $u) { return $null }
    if (-not $u.Trim()) { $u = $script:BaseUrl }
    $label = 'API key (hidden)'
    if ($script:ApiKey) { $label = 'API key (hidden, Enter keeps the current one)' }
    $piped = $false; try { $piped = [Console]::IsInputRedirected } catch { }
    $k = ''; $secure = $null
    if ($piped) { $k = Read-Host $label; if ($null -eq $k) { return $null } } else { $secure = Read-Host $label -AsSecureString }
    if ($secure) { $cred = New-Object System.Management.Automation.PSCredential('key', $secure); $k = $cred.GetNetworkCredential().Password }
    if (-not $k) { $k = $script:ApiKey }
    return @{ url = $u; key = $k; auth = $Auth }
}

function Confirm-Config([string]$err, [string]$failed) {
    # Checks URL + key with GET /models. On failure: says which request failed and why, asks
    # again (page form or console prompts) and saves what was typed once it works.
    $typed = $false
    while ($true) {
        $ask = [bool]$err
        if (-not $ask -and (-not $script:BaseUrl -or -not $script:ApiKey)) { $ask = $true }
        if (-not $ask) {
            try {
                $script:CatalogIds = @()
                $script:ApiTimeout = 20; $script:ApiTries = 1   # a quick probe: no 15-minute hang on a dead URL
                try { Get-Catalog } catch { $e1 = '' + $_.Exception.Message; if ($e1 -notmatch '^HTTP \d+ from ') { $e1 = "the reply is not a model list (wrong URL?): $e1" }; throw $e1 }
                if ($script:CatalogIds.Count -eq 0) { throw 'the reply lists no chat models (wrong URL?)' }
            }
            catch {
                $err = '' + $_.Exception.Message
                if ($script:ModelGiven -and $err -match '^HTTP 404 ') { Say "  cannot list models ($err); continuing with $Model" 'DarkYellow'; $err = '' }
            }
            finally { $script:ApiTimeout = 900; $script:ApiTries = 4 }
            if (-not $err) {
                if ($typed) {
                    $script:Cfg.api_url = $script:BaseUrl; $script:Cfg.api_key = $script:ApiKey; $script:Cfg.auth = $Auth
                    Save-Config $true
                }
                return
            }
        }
        $tail = '' + $script:ApiKey
        if ($tail.Length -gt 4) { $tail = $tail.Substring($tail.Length - 4) }
        $req = "GET $($script:ApiRoot)/models ($Auth key ending $tail)"
        if ($failed) { $req = "$failed ($Auth key ending $tail)"; $failed = '' }
        if ($err) { Say "  request failed: $req" 'Red'; Say "  $err" 'Red' }
        if ($Prompt) { Say '  check HARNESS_API_URL and the key (or harness-lite.json)' 'Red'; exit 1 }
        $v = Read-Config $req $err
        if ($null -eq $v) { if ($script:B) { $script:B.Stop = $true }; exit 1 }
        Set-ApiConfig $v.url $v.key $v.auth
        $typed = $true
        $err = ''
    }
}

if (-not $urlGiven -and $script:Cfg.api_url) { $BaseUrl = $script:Cfg.api_url }
$envKey = $env:HARNESS_API_KEY
if ($script:Cfg.api_key -and (-not $envKey -or -not $urlGiven)) { $envKey = $script:Cfg.api_key }
Set-ApiConfig $BaseUrl $envKey ''

if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "Root folder not found: $Root" }
function Get-CanonicalDir([string]$p) {
    # opencode: fs.realpathSync.native(path.resolve(dir)) - on-disk letter case,
    # long (not 8.3) names, upper-case drive letter, C:\ for a drive root.
    # (Junctions, symlinks and subst drives are not resolved here.)
    if (-not $script:OnWindows) { return $p }
    if ($p.StartsWith('\\')) { return $p }   # UNC share: leave as given
    try {
        $p = $p.Replace('/', '\')
        if ($p -match '^[a-zA-Z]:\\?$') { return $p.Substring(0, 1).ToUpper() + ':\' }
        $cur = $p.TrimEnd('\')
        $parts = @()
        $guard = 0
        while ($guard -lt 200) {
            $guard++
            $parent = Split-Path -Parent $cur
            if (-not $parent) { break }
            $leaf = Split-Path -Leaf $cur
            $hit = @(Get-ChildItem -LiteralPath $parent -Filter $leaf -Force -ErrorAction SilentlyContinue)
            if ($hit.Count -eq 1) { $leaf = '' + $hit[0].Name }
            $parts = @($leaf) + $parts
            $cur = $parent
        }
        if ($cur -match '^[a-z]:') { $cur = $cur.Substring(0, 1).ToUpper() + $cur.Substring(1) }
        if ($parts.Count -eq 0) { return $cur }
        return $cur.TrimEnd('\') + '\' + ($parts -join '\')
    }
    catch { return $p }
}
$script:RootFull = (Resolve-Path -LiteralPath $Root).ProviderPath
if ($script:OnWindows) { $script:RootFull = Get-CanonicalDir $script:RootFull }
if ($script:RootFull.Length -gt 3 -or -not $script:OnWindows) { $script:RootFull = $script:RootFull.TrimEnd('\', '/') }
if (-not $script:RootFull) { $script:RootFull = '/' }
# opencode's home (os.homedir()) and config dir (XDG_CONFIG_HOME or ~/.config).
$script:HomeDir = $HOME
if ($script:OnWindows -and $env:USERPROFILE) { $script:HomeDir = $env:USERPROFILE }
$script:OcConfig = Join-Path (Join-Path $script:HomeDir '.config') 'opencode'
if ($env:XDG_CONFIG_HOME) { $script:OcConfig = Join-Path $env:XDG_CONFIG_HOME 'opencode' }

$tmpBase = $env:TEMP
if (-not $tmpBase) { $tmpBase = $env:TMPDIR }
if (-not $tmpBase) { $tmpBase = '/tmp' }
$script:TmpDir = Join-Path $tmpBase 'harness-lite'
$script:OcTmp = Join-Path $tmpBase 'opencode'
if ($script:OnWindows) { $script:OcTmp = Join-Path (Get-CanonicalDir $tmpBase) 'opencode' }
if (-not (Test-Path -LiteralPath $script:TmpDir)) { $null = New-Item -ItemType Directory -Path $script:TmpDir -Force }
$stateBase = $env:LOCALAPPDATA
if (-not $stateBase) { $stateBase = $HOME }
$script:StateDir = Join-Path $stateBase 'harness-lite'
try { if (-not (Test-Path -LiteralPath $script:StateDir)) { $null = New-Item -ItemType Directory -Path $script:StateDir -Force } } catch { $script:StateDir = $script:TmpDir }
$rootKey = $script:RootFull -replace '[^A-Za-z0-9]+', '_'
if ($rootKey.Length -gt 100) { $rootKey = $rootKey.Substring($rootKey.Length - 100) }
$script:SessionFile = Join-Path $script:StateDir "session-$rootKey.json"
$script:LastModelFile = Join-Path $script:StateDir 'last-model.txt'
$script:PsExe = (Get-Process -Id $PID).Path

#@@TEXTS@@

# ---------------------------------------------------------------- shell
function Test-Shell([string]$exe) {
    # A bash.exe that exists can still be blocked (AppLocker); run it once to be sure.
    $f = Join-Path $script:TmpDir ('probe-' + (Get-Random) + '.out')
    $ok = $false
    try {
        $p = Start-Process -FilePath $exe -ArgumentList '-c "echo hl-probe-ok"' -NoNewWindow -PassThru -RedirectStandardOutput $f
        $t0 = Get-Date
        while (-not $p.HasExited -and ((Get-Date) - $t0).TotalSeconds -lt 15) { Start-Sleep -Milliseconds 100 }
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 100
        if (Test-Path -LiteralPath $f) { $ok = ('' + (Get-Content -LiteralPath $f -Raw)) -match 'hl-probe-ok' }
    }
    catch { $ok = $false }
    if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
    return $ok
}

function Find-Shell {
    $want = '' + $env:HARNESS_LITE_SHELL
    if ($want -match '^(?i)(powershell|pwsh)$') { return @{ kind = 'powershell'; exe = $script:PsExe; why = 'HARNESS_LITE_SHELL' } }
    $cands = @()
    if ($want) { $cands += $want }
    elseif ($script:OnWindows) {
        foreach ($b in @($env:ProgramFiles, $env:ProgramW6432, ${env:ProgramFiles(x86)}, (Join-Path ('' + $env:LOCALAPPDATA) 'Programs'))) {
            if ($b) { $cands += (Join-Path $b 'Git\bin\bash.exe') }
        }
        $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($git) { $cands += (Join-Path (Split-Path -Parent (Split-Path -Parent $git.Source)) 'bin\bash.exe') }
    }
    else { $cands += '/bin/bash' }
    $seen = @{}
    foreach ($c in $cands) {
        if (-not $c -or $seen.ContainsKey($c.ToLower())) { continue }
        $seen[$c.ToLower()] = 1
        if ($c -match '(?i)\\(System32|SysWOW64|Sysnative)\\') { continue }   # that bash is WSL, not Git Bash
        if (-not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        if (Test-Shell $c) { return @{ kind = 'bash'; exe = $c } }
    }
    return @{ kind = 'powershell'; exe = $script:PsExe; why = 'no usable Git Bash found' }
}

# ---------------------------------------------------------------- JSON helpers
function ConvertTo-AsciiJson([string]$json) {
    # Escape every non-ASCII UTF-16 unit as \uXXXX so the body is plain ASCII
    # and no PowerShell version can mis-encode it.
    if ($json -notmatch '[^\x00-\x7f]') { return $json }
    $seen = @{}
    foreach ($m in [regex]::Matches($json, '[^\x00-\x7f]')) {
        $c = $m.Value
        if (-not $seen.ContainsKey($c)) {
            $seen[$c] = 1
            $json = $json.Replace($c, ('\u{0:x4}' -f [int][char]$c))
        }
    }
    return $json
}

function ConvertTo-JsonText([string]$s, [bool]$ascii) {
    $s = $s.Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n').Replace("`r", '\r').Replace("`t", '\t')
    $s = $s.Replace([string][char]8, '\b').Replace([string][char]12, '\f')
    if ($s -match '[\x00-\x1f]') {
        foreach ($m in [regex]::Matches($s, '[\x00-\x1f]')) { $s = $s.Replace($m.Value, ('\u{0:x4}' -f [int][char]$m.Value)) }
    }
    if ($ascii) { $s = ConvertTo-AsciiJson $s }
    return '"' + $s + '"'
}

function ConvertTo-PyJson($v, [bool]$ascii, [bool]$sort, [bool]$compact = $false) {
    # Python json.dumps layout (", " and ": "), optionally with sorted keys; or,
    # with $compact, JavaScript JSON.stringify layout (what opencode sends back).
    $cs = ', '
    $ks = ': '
    if ($compact) { $cs = ','; $ks = ':' }
    if ($null -eq $v) { return 'null' }
    if ($v -is [string]) { return (ConvertTo-JsonText $v $ascii) }
    if ($v -is [bool]) { if ($v) { return 'true' }; return 'false' }
    if ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal]) { return ('' + $v) }
    if ($v -is [hashtable]) {
        $keys = @($v.Keys)
        if ($sort) { $keys = @($keys | Sort-Object -CaseSensitive) }
        $parts = @()
        foreach ($k in $keys) { $parts += ((ConvertTo-JsonText ('' + $k) $ascii) + $ks + (ConvertTo-PyJson $v[$k] $ascii $sort $compact)) }
        return '{' + ($parts -join $cs) + '}'
    }
    if ($v -is [array]) {
        $parts = @()
        foreach ($x in $v) { $parts += (ConvertTo-PyJson $x $ascii $sort $compact) }
        return '[' + ($parts -join $cs) + ']'
    }
    $props = @($v.PSObject.Properties)
    if ($sort) { $props = @($props | Sort-Object -Property Name -CaseSensitive) }
    $parts = @()
    foreach ($p in $props) { $parts += ((ConvertTo-JsonText $p.Name $ascii) + $ks + (ConvertTo-PyJson $p.Value $ascii $sort $compact)) }
    return '{' + ($parts -join $cs) + '}'
}

function Find-JsonEnd([string]$s, [int]$start) {
    # Index of the brace closing the object that opens at $start, or -1.
    if ($start -ge $s.Length -or $s[$start] -ne '{') { return -1 }
    $depth = 0
    $inStr = $false
    $esc = $false
    for ($i = $start; $i -lt $s.Length; $i++) {
        $ch = $s[$i]
        if ($esc) { $esc = $false; continue }
        if ($inStr) {
            if ($ch -eq '\') { $esc = $true }
            elseif ($ch -eq '"') { $inStr = $false }
            continue
        }
        if ($ch -eq '"') { $inStr = $true }
        elseif ($ch -eq '{') { $depth++ }
        elseif ($ch -eq '}') { $depth--; if ($depth -eq 0) { return $i } }
    }
    return -1
}

function Repair-JsonStrings([string]$s) {
    # json.loads(strict=False): raw newlines/tabs inside JSON strings are allowed.
    $out = @()
    $inStr = $false
    $esc = $false
    $last = 0
    for ($i = 0; $i -lt $s.Length; $i++) {
        $ch = $s[$i]
        if (-not $inStr) { if ($ch -eq '"') { $inStr = $true }; continue }
        if ($esc) { $esc = $false; continue }
        if ($ch -eq '\') { $esc = $true; continue }
        if ($ch -eq '"') { $inStr = $false; continue }
        $code = [int]$ch
        if ($code -lt 32) {
            $out += $s.Substring($last, $i - $last)
            if ($code -eq 10) { $out += '\n' } elseif ($code -eq 13) { $out += '\r' } elseif ($code -eq 9) { $out += '\t' } else { $out += ('\u{0:x4}' -f $code) }
            $last = $i + 1
        }
    }
    $out += $s.Substring($last)
    return ($out -join '')
}

function Test-BadEscape([string]$s) {
    # A backslash escape JSON does not allow (\x, \a, \v, ...) inside a string.
    $inStr = $false
    for ($i = 0; $i -lt $s.Length; $i++) {
        $ch = $s[$i]
        if (-not $inStr) { if ($ch -eq '"') { $inStr = $true }; continue }
        if ($ch -eq '"') { $inStr = $false; continue }
        if ($ch -eq '\') {
            if ($i + 1 -ge $s.Length) { return $true }
            if ('"\/bfnrtu'.IndexOf($s[$i + 1]) -lt 0) { return $true }
            $i++
        }
    }
    return $false
}

function ConvertFrom-ArgsJson([string]$json) {
    if (-not $json.Trim()) { return ('{}' | ConvertFrom-Json) }
    if (Test-BadEscape $json) { throw 'invalid \escape in JSON' }
    try { return ($json | ConvertFrom-Json) }
    catch { return ((Repair-JsonStrings $json) | ConvertFrom-Json) }
}

function Get-Prop($o, [string]$name) {
    if ($null -eq $o) { return $null }
    if ($o -is [hashtable]) { return $o[$name] }
    $p = $o.PSObject.Properties[$name]
    if ($p) { return $p.Value }
    return $null
}

function Test-Prop($o, [string]$name) {
    if ($null -eq $o -or $o -is [string]) { return $false }
    if ($o -is [hashtable]) { return $o.ContainsKey($name) }
    return $null -ne $o.PSObject.Properties[$name]
}

function Find-Str([string]$s, [string]$sub, [int]$start) {
    # Ordinal IndexOf (the one-argument form is culture-aware and skips zero-width characters).
    if ($start -gt $s.Length) { return -1 }
    return $s.IndexOf($sub, $start, $s.Length - $start, 'Ordinal')
}

function Format-Thousands([long]$n) { return (('' + $n) -replace '(?<=\d)(?=(\d{3})+$)', ',') }

# ---------------------------------------------------------------- HTTP
function Get-RetryAfter($err) {
    $secs = 0
    try { $v = $err.Exception.Response.Headers.RetryAfter.Delta.TotalSeconds; if ($v) { $secs = [int]$v } } catch { }
    if (-not $secs) { try { $v = $err.Exception.Response.Headers['Retry-After']; if ($v) { $secs = [int]$v } } catch { } }
    return $secs
}

$script:ApiTimeout = 900
$script:ApiTries = 4
function Invoke-Api([string]$method, [string]$path, [string]$bodyJson) {
    $headers = @{}
    if ($Auth -eq 'api-key') { $headers['api-key'] = $script:ApiKey } else { $headers['Authorization'] = "Bearer $($script:ApiKey)" }
    $out = Join-Path $script:TmpDir ('resp-' + (Get-Random) + '.json')
    $p = @{ Uri = "$($script:ApiRoot)$path"; Method = $method; Headers = $headers; OutFile = $out
        PassThru = $true; UseBasicParsing = $true; TimeoutSec = $script:ApiTimeout }
    if ($bodyJson) { $p['Body'] = (ConvertTo-AsciiJson $bodyJson); $p['ContentType'] = 'application/json; charset=utf-8' }
    if ($bodyJson -and $env:HARNESS_LITE_DUMP) {
        # Debug: keep every upstream request body, like the proxy's OUTPUT_DIR *_02_API_Request.json dumps.
        try {
            if (-not (Test-Path -LiteralPath $env:HARNESS_LITE_DUMP)) { New-Item -ItemType Directory -Path $env:HARNESS_LITE_DUMP -Force | Out-Null }
            $script:DumpN = 1 + $script:DumpN
            $dumpName = (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '_' + ('{0:D4}' -f $script:DumpN) + '_lite_request.json'
            Set-Content -LiteralPath (Join-Path $env:HARNESS_LITE_DUMP $dumpName) -Value $p['Body'] -Encoding ASCII -NoNewline
        } catch { }
    }
    $attempt = 0
    while ($true) {
        $attempt++
        try { $r = Invoke-WebRequest @p; break }
        catch {
            $status = 0
            if ($_.Exception.Response) { try { $status = [int]$_.Exception.Response.StatusCode } catch { } }
            $detail = $_.ErrorDetails.Message
            if (-not $detail) { $detail = $_.Exception.Message }
            $detail = ($detail -replace '<[^>]+>', ' ' -replace '\s+', ' ').Trim()
            if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) + ' ...' }
            if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
            $wait = 0
            if ($status -eq 502 -or $status -eq 503 -or $status -eq 504) { $wait = $attempt * 10 }
            if ($status -eq 429 -and $detail -notmatch 'allowance|24-hour|per day|daily') {
                $wait = Get-RetryAfter $_
                if (-not $wait) { $wait = $attempt * 15 }
                if ($wait -gt 120) { $wait = 0 }
            }
            if ($wait -gt 0 -and $attempt -lt $script:ApiTries) {
                $label = "upstream $status"
                if ($status -eq 429) { $label = 'rate limited (per-minute limit)' }
                Say "  $label; retrying in $wait s" 'DarkYellow'
                Start-Sleep -Seconds $wait
                continue
            }
            throw "HTTP $status from $path : $detail"
        }
    }
    $text = Get-Content -LiteralPath $out -Raw -Encoding UTF8
    Remove-Item -LiteralPath $out -Force
    $left = $r.Headers['X-RateLimit-Remaining-Credits-Day']
    if ($left) { try { $script:CreditsLeft = [long](@($left)[0]) } catch { } }
    return @{ data = ($text | ConvertFrom-Json); headers = $r.Headers }
}

function Get-MessageText($msg) {
    $c = Get-Prop $msg 'content'
    if ($null -eq $c) { return '' }
    if ($c -is [string]) { return $c }
    $out = ''
    foreach ($p in @($c)) { if ($p -is [string]) { $out += $p } elseif ($p.text) { $out += $p.text } }
    return $out
}

# ---------------------------------------------------------------- files
function Get-FullPath([string]$p) {
    # Absolute, normalized path; relative paths are under the working folder.
    $p = ('' + $p).Trim()
    if (-not $p) { $p = '.' }
    if ($script:OnWindows -and $p -match '^/([a-zA-Z])(/|$)') { $rest = ''; if ($p.Length -gt 3) { $rest = $p.Substring(3) }; $p = $Matches[1] + ':/' + $rest }
    $abs = ($p -match '^[a-zA-Z]:[\\/]') -or ($p -match '^[\\/]')
    if ($abs) { $full = $p } else { $full = $script:RootFull + $script:Sep + $p }
    $lead = ''
    if ($full -match '^[\\/]{2}[^\\/]') { $lead = $script:Sep + $script:Sep; $full = $full.Substring(2) }
    elseif ($full -match '^([a-zA-Z]:)') { $lead = $Matches[1].ToUpper() + $script:Sep; $full = $full.Substring(2) }
    elseif ($full -match '^[\\/]') { $lead = $script:Sep }
    $parts = @()
    foreach ($seg in ($full -split '[\\/]+')) {
        if ($seg -eq '' -or $seg -eq '.') { continue }
        if ($seg -eq '..') { if ($parts.Count -gt 1) { $parts = @($parts[0..($parts.Count - 2)]) } else { $parts = @() }; continue }
        $parts += $seg
    }
    return $lead + ($parts -join $script:Sep)
}

function Test-InRoot([string]$full) {
    $r = $script:RootFull
    $a = $full
    if ($script:OnWindows) { $a = $a.ToLower(); $r = $r.ToLower() }
    return ($a -eq $r -or $a.StartsWith($r.TrimEnd('\', '/') + $script:Sep))
}

function Request-Approval([string]$what) {
    # y / n / a(lways) / free text; returns @{ ok; note }.
    if ($script:AutoApproveOn) { return @{ ok = $true; note = '' } }
    W "  $what " 'Yellow'
    WL ("y yes $($script:G.mid) n no $($script:G.mid) a always (this session) $($script:G.mid) or type what to do instead") 'DarkGray'
    W ('  ' + $script:G.prompt + ' ') 'Yellow'
    $ans = Read-Host
    if ($null -eq $ans) { $ans = '' }
    return (Resolve-Approval $ans)
}

function Resolve-Approval([string]$ans) {
    $ans = $ans.Trim()
    if ($ans -match '^(a|always)$') { $script:AutoApproveOn = $true; Say '  auto-approve is on for this session (/auto turns it off)' 'DarkGray'; return @{ ok = $true; note = '' } }
    if ($ans -match '^(y|yes)$') { return @{ ok = $true; note = '' } }
    Show-Sub 'declined' 'DarkYellow'
    if ($ans -and $ans -notmatch '^(n|no)$') { return @{ ok = $false; note = $ans } }
    return @{ ok = $false; note = '' }
}

function Get-DeclineText($ap) {
    $s = 'The user rejected permission to use this specific tool call.'
    if ($ap.note) { $s += " The user said: $($ap.note)" }
    return $s
}

function Resolve-ToolPath([string]$p) {
    $full = Get-FullPath $p
    if (-not (Test-InRoot $full)) {
        $ap = Request-Approval "Allow access outside the working folder ($full)?"
        if (-not $ap.ok) { throw (Get-DeclineText $ap) }
    }
    return $full
}

function Read-TextFile([string]$full) {
    if ($script:IsPS5) { $head = Get-Content -LiteralPath $full -Encoding Byte -TotalCount 3 }
    else { $head = Get-Content -LiteralPath $full -AsByteStream -TotalCount 3 }
    $bom = ($head.Count -ge 3 -and $head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF)
    $text = Get-Content -LiteralPath $full -Raw -Encoding UTF8
    if ($null -eq $text) { $text = '' }
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    if ($text.Contains([string][char]0)) { throw "Cannot read binary file: $full" }
    return @{ text = $text; bom = $bom; crlf = $text.Contains("`r`n") }
}

function Write-TextFile([string]$full, [string]$text, [bool]$bom) {
    $parent = Split-Path -Parent $full
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { $null = New-Item -ItemType Directory -Path $parent -Force }
    if (-not $script:IsPS5) {
        $enc = 'utf8NoBOM'
        if ($bom) { $enc = 'utf8BOM' }
        Set-Content -LiteralPath $full -Value $text -Encoding $enc -NoNewline
        return
    }
    # 5.1 can only write UTF-8 with a BOM; strip it afterwards unless wanted.
    Set-Content -LiteralPath $full -Value $text -Encoding UTF8 -NoNewline
    if (-not $bom) {
        $bytes = Get-Content -LiteralPath $full -Encoding Byte -ReadCount 0
        if ($bytes.Count -le 3) { Clear-Content -LiteralPath $full }
        else { Set-Content -LiteralPath $full -Value $bytes[3..($bytes.Count - 1)] -Encoding Byte }
    }
}

function Limit-Text([string]$s) {
    if ($s.Length -le $script:MaxToolOutput) { return $s }
    return $s.Substring(0, $script:MaxToolOutput) + "`n`n... [output truncated: $($s.Length - $script:MaxToolOutput) more chars. Narrow the request (offset/limit, a narrower pattern, or filter the command output).]"
}

function Get-Lines([string]$s) {
    $l = @($s -split "`r?`n")
    if ($l.Count -gt 1 -and $l[$l.Count - 1] -eq '') { $l = @($l[0..($l.Count - 2)]) }
    return $l
}

$script:Undo = @()
function Push-Undo([string]$full) {
    if (Test-Path -LiteralPath $full -PathType Leaf) {
        $f = Read-TextFile $full
        $script:Undo += @{ path = $full; existed = $true; text = $f.text; bom = $f.bom }
    }
    else { $script:Undo += @{ path = $full; existed = $false } }
}

$script:SkipDirs = '[\\/](\.git|node_modules|\.venv|venv|__pycache__|\.next|dist[\\/]node_modules|bin[\\/]Debug|bin[\\/]Release|obj)([\\/]|$)'

function Get-ProjectFiles([string]$base) {
    return @(Get-ChildItem -LiteralPath $base -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch $script:SkipDirs })
}

function Get-RelPath([string]$full) {
    if (-not (Test-InRoot $full)) { return $full }
    if ($full.Length -le $script:RootFull.Length) { return '.' }
    return $full.Substring($script:RootFull.TrimEnd('\', '/').Length + 1)
}

function Convert-GlobToRegex([string]$g) {
    $g = $g.Replace('\', '/')
    $sb = ''
    $i = 0
    $brace = 0
    while ($i -lt $g.Length) {
        $c = [string]$g[$i]
        if ($c -eq '*') {
            if ($i + 1 -lt $g.Length -and $g[$i + 1] -eq '*') {
                $i += 2
                if ($i -lt $g.Length -and $g[$i] -eq '/') { $i++; $sb += '(?:.*/)?' } else { $sb += '.*' }
                continue
            }
            $sb += '[^/]*'
        }
        elseif ($c -eq '?') { $sb += '[^/]' }
        elseif ($c -eq '{') { $sb += '(?:'; $brace++ }
        elseif ($c -eq '}' -and $brace -gt 0) { $sb += ')'; $brace-- }
        elseif ($c -eq ',' -and $brace -gt 0) { $sb += '|' }
        elseif ($c -eq '[') {
            $j = $g.IndexOf(']', $i + 1)
            if ($j -gt $i + 1) { $cls = $g.Substring($i + 1, $j - $i - 1); if ($cls.StartsWith('!')) { $cls = '^' + $cls.Substring(1) }; $sb += '[' + $cls.Replace('\', '\\') + ']'; $i = $j }
            else { $sb += '\[' }
        }
        else { $sb += [regex]::Escape($c) }
        $i++
    }
    return '^' + $sb + '$'
}

function Test-GlobMatch([string]$rx, [string]$s) {
    if ($script:OnWindows) { return $s -match $rx }
    return $s -cmatch $rx
}

# ---------------------------------------------------------------- tools
$script:ReadFiles = @{}
function Get-FileKey([string]$full) { if ($script:OnWindows) { return $full.ToLower() }; return $full }

function Tool-Read($a) {
    $full = Resolve-ToolPath (Get-Prop $a 'filePath')
    $offset = 1
    if (Get-Prop $a 'offset') { $offset = [int](Get-Prop $a 'offset') }
    if ($offset -lt 1) { $offset = 1 }
    $limit = 2000
    if (Test-Prop $a 'limit') { $limit = [int](Get-Prop $a 'limit') }
    if ($limit -lt 0) { $limit = 0 }
    if (Test-Path -LiteralPath $full -PathType Container) {
        $items = @(Get-ChildItem -LiteralPath $full -Force -ErrorAction SilentlyContinue)
        $names = @()
        foreach ($i in $items) { if ($i.PSIsContainer) { $names += ($i.Name + '/') } else { $names += $i.Name } }
        $names = @($names | Sort-Object)
        $from = $offset - 1
        $to = $from + $limit - 1
        if ($to -ge $names.Count) { $to = $names.Count - 1 }
        $shown = @()
        if ($from -le $to) { $shown = @($names[$from..$to]) }
        $tail = "`n($($names.Count) entries)"
        if ($to -lt $names.Count - 1) { $tail = "`n(Showing $($shown.Count) of $($names.Count) entries. Use 'offset' parameter to read beyond entry $($offset + $shown.Count))" }
        Show-Sub ((Get-Plural $names.Count 'entry').Replace('entrys', 'entries'))
        return (@("<path>$full</path>", '<type>directory</type>', '<entries>', ($shown -join "`n"), $tail, '</entries>') -join "`n")
    }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        $hint = ''
        $dir = Split-Path -Parent $full
        $leaf = Split-Path -Leaf $full
        if ($dir -and (Test-Path -LiteralPath $dir -PathType Container)) {
            $lc = $leaf.ToLower()
            $near = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue | Where-Object { $n = $_.Name.ToLower(); $n.Contains($lc) -or $lc.Contains($n) } | Select-Object -First 3)
            if ($near.Count -gt 0) { $hint = "`n`nDid you mean one of these?`n" + (($near | ForEach-Object { $_.FullName }) -join "`n") }
        }
        throw "File not found: $full$hint"
    }
    if (Test-BinaryFile $full) { throw "Cannot read binary file: $full" }
    $f = Read-TextFile $full
    $script:ReadFiles[(Get-FileKey $full)] = 1
    $lines = @(Get-Lines $f.text)
    if ($f.text.Length -eq 0) { $lines = @() }
    $count = $lines.Count
    if ($count -gt 0 -and $offset -gt $count) { throw "Offset $offset is out of range for this file ($count lines)" }
    $out = @()
    $bytes = 0
    $cut = $false
    $n = $offset
    for (; $n -le $count -and $n -lt $offset + $limit; $n++) {
        $l = $lines[$n - 1]
        if ($l.Length -gt 2000) { $l = $l.Substring(0, 2000) + '... (line truncated to 2000 chars)' }
        # opencode counts the UTF-8 bytes of the line (not the "N: " prefix), plus 1 per line after the first.
        $bl = Get-Utf8Len $l
        if ($out.Count -gt 0) { $bl++ }
        if ($bytes + $bl -gt 51200) { $cut = $true; break }
        $out += "${n}: $l"
        $bytes += $bl
    }
    $last = $offset + $out.Count - 1
    $r = "<path>$full</path>`n<type>file</type>`n<content>`n" + ($out -join "`n")
    if ($cut) { $r += "`n`n(Output capped at 50 KB. Showing lines $offset-$last. Use offset=$($last + 1) to continue.)" }
    elseif ($last -lt $count) { $r += "`n`n(Showing lines $offset-$last of $count. Use offset=$($last + 1) to continue.)" }
    else { $r += "`n`n(End of file - total $count lines)" }
    $r += "`n</content>"
    if ($count -eq 0) { Show-Sub 'empty file' } else { Show-Sub "lines $offset-$last of $count" }
    return $r
}

function Get-Utf8Len([string]$s) {
    if ($s -notmatch '[^\x00-\x7f]') { return $s.Length }
    return $s.Length + [regex]::Matches($s, '[\u0080-\uffff]').Count + [regex]::Matches($s, '[\u0800-\uffff]').Count - 2 * [regex]::Matches($s, '[\ud800-\udbff][\udc00-\udfff]').Count
}

$script:BinaryExt = @{}
foreach ($e in ('.zip .tar .gz .exe .dll .so .class .jar .war .7z .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .bin .dat .obj .o .a .lib .wasm .pyc .pyo' -split ' ')) { $script:BinaryExt[$e] = 1 }

function Test-BinaryFile([string]$full) {
    # opencode's read: binary by extension, by a NUL byte, or when more than
    # 30% of the first 4096 bytes are control bytes.
    $m = [regex]::Match($full, '\.[^.\\/]+$')
    if ($m.Success -and $script:BinaryExt.ContainsKey($m.Value.ToLower())) { return $true }
    try {
        if ($script:IsPS5) { $head = @(Get-Content -LiteralPath $full -Encoding Byte -TotalCount 4096 -ErrorAction Stop) }
        else { $head = @(Get-Content -LiteralPath $full -AsByteStream -TotalCount 4096 -ErrorAction Stop) }
    }
    catch { return $false }
    if ($head.Count -eq 0) { return $false }
    $ctl = 0
    foreach ($b in $head) {
        if ($b -eq 0) { return $true }
        if ($b -lt 9 -or ($b -gt 13 -and $b -lt 32)) { $ctl++ }
    }
    return ($ctl * 10 -gt $head.Count * 3)
}

function Show-Diff($oldLines, $newLines) {
    $k = 0
    foreach ($l in $oldLines) { if ($k -ge 8) { Show-More "  ... $(@($oldLines).Count - 8) more removed"; break }; Show-More ('- ' + $l) 'Red'; $k++ }
    $k = 0
    foreach ($l in $newLines) { if ($k -ge 8) { Show-More "  ... $(@($newLines).Count - 8) more added"; break }; Show-More ('+ ' + $l) 'Green'; $k++ }
}

function Tool-Write($a) {
    $full = Resolve-ToolPath (Get-Prop $a 'filePath')
    if (Test-Path -LiteralPath $full -PathType Container) { throw "Path is a directory, not a file: $full" }
    $content = '' + (Get-Prop $a 'content')
    $bom = $false
    $existed = Test-Path -LiteralPath $full -PathType Leaf
    if ($existed) {
        $old = Read-TextFile $full
        $bom = $old.bom
        $content = $content -replace "`r`n", "`n"
        if ($old.crlf) { $content = $content -replace "`n", "`r`n" }
    }
    Push-Undo $full
    Write-TextFile $full $content $bom
    $script:ReadFiles[(Get-FileKey $full)] = 1
    $lines = @(Get-Lines $content)
    $verb = 'created'
    if ($existed) { $verb = 'overwrote' }
    Show-Sub "$verb ($(Get-Plural $lines.Count 'line'))" 'Green'
    $shown = 0
    foreach ($l in $lines) {
        if ($shown -ge 6) { Show-More "... $($lines.Count - 6) more lines"; break }
        Show-More ('+ ' + $l) 'DarkGreen'
        $shown++
    }
    return 'Wrote file successfully.'
}

function Find-TrimmedBlock([string]$text, [string]$old) {
    # opencode's line-trimmed fallback: the same lines, ignoring leading and
    # trailing whitespace on each. Returns @(start, length) pairs.
    $fl = @($text -split "`n")
    $ol = @($old -split "`n")
    if ($ol.Count -gt 1 -and $ol[$ol.Count - 1] -eq '') { $ol = @($ol[0..($ol.Count - 2)]) }
    $hits = @()
    if ($ol.Count -eq 0) { return $hits }
    $starts = @()
    $pos = 0
    foreach ($l in $fl) { $starts += $pos; $pos += $l.Length + 1 }
    for ($i = 0; $i -le $fl.Count - $ol.Count; $i++) {
        $ok = $true
        for ($j = 0; $j -lt $ol.Count; $j++) { if ($fl[$i + $j].Trim() -cne $ol[$j].Trim()) { $ok = $false; break } }
        if ($ok) {
            $endLine = $i + $ol.Count - 1
            $len = $starts[$endLine] + $fl[$endLine].Length - $starts[$i]
            $hits += , @($starts[$i], $len)
        }
    }
    return $hits
}

function Tool-Edit($a) {
    $full = Resolve-ToolPath (Get-Prop $a 'filePath')
    $old = ('' + (Get-Prop $a 'oldString')) -replace "`r`n", "`n"
    $new = ('' + (Get-Prop $a 'newString')) -replace "`r`n", "`n"
    $all = [bool](Get-Prop $a 'replaceAll')
    if ($old -ceq $new) { throw 'No changes to apply: oldString and newString are identical.' }
    $exists = Test-Path -LiteralPath $full -PathType Leaf
    if ($old -eq '') {
        if ($exists) { throw 'oldString cannot be empty when editing an existing file. Provide the exact text to replace, or use write for an intentional full-file replacement.' }
        $bom = $false
        $crlf = $false
        if ($exists) { $f0 = Read-TextFile $full; $bom = $f0.bom; $crlf = $f0.crlf }
        $out = $new
        if ($crlf) { $out = $out -replace "`n", "`r`n" }
        Push-Undo $full
        Write-TextFile $full $out $bom
        $script:ReadFiles[(Get-FileKey $full)] = 1
        Show-Sub "wrote $(Get-Plural @(Get-Lines $new).Count 'line')" 'Green'
        return 'Edit applied successfully.'
    }
    if (-not $exists) { throw "File $full not found" }
    $f = Read-TextFile $full
    $text = $f.text -replace "`r`n", "`n"
    $hits = @()
    $i = (Find-Str $text $old 0)
    while ($i -ge 0) { $hits += , @($i, $old.Length); $i = (Find-Str $text $old ($i + $old.Length)) }
    if ($hits.Count -eq 0) { $hits = @(Find-TrimmedBlock $text $old) }
    if ($hits.Count -eq 0) { throw 'Could not find oldString in the file. It must match exactly, including whitespace, indentation, and line endings.' }
    if ($hits.Count -gt 1 -and -not $all) { throw 'Found multiple matches for oldString. Provide more surrounding context to make the match unique.' }
    $firstLine = @($text.Substring(0, $hits[0][0]) -split "`n").Count
    $removed = @()
    for ($k = $hits.Count - 1; $k -ge 0; $k--) {
        $h = $hits[$k]
        if ($k -eq 0) { $removed = @(Get-Lines $text.Substring($h[0], $h[1])) }
        $text = $text.Substring(0, $h[0]) + $new + $text.Substring($h[0] + $h[1])
    }
    if ($f.crlf) { $text = $text -replace "`n", "`r`n" }
    Push-Undo $full
    Write-TextFile $full $text $f.bom
    $added = @(Get-Lines $new)
    if ($new.Length -eq 0) { $added = @() }
    $where = "line $firstLine"
    if ($hits.Count -gt 1) { $where = "$($hits.Count) places, first at line $firstLine" }
    Show-Sub "$where  -$($removed.Count) +$($added.Count)" 'Green'
    Show-Diff $removed $added
    return 'Edit applied successfully.'
}

function Get-SearchBase($a) {
    $p = Get-Prop $a 'path'
    if (-not $p -or $p -eq 'undefined' -or $p -eq 'null') { return $script:RootFull }
    $full = Resolve-ToolPath $p
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "Directory not found: $full" }
    return $full
}

function Tool-Glob($a) {
    $gp = Get-Prop $a 'path'
    if ($gp -and $gp -ne 'undefined' -and $gp -ne 'null') {
        $gfull = Get-FullPath $gp
        if (Test-Path -LiteralPath $gfull -PathType Leaf) { throw "glob path must be a directory: $gfull" }
        if (-not (Test-Path -LiteralPath $gfull -PathType Container)) { throw 'ripgrep execution failed' }
    }
    $base = Get-SearchBase $a
    $pat = '' + (Get-Prop $a 'pattern')
    if (-not $pat) { throw 'pattern is empty' }
    $rx = Convert-GlobToRegex $pat
    $byName = -not $pat.Replace('\', '/').Contains('/')
    $hits = @()
    foreach ($f in (Get-ProjectFiles $base)) {
        $rel = $f.FullName.Substring($base.TrimEnd('\', '/').Length).TrimStart('\', '/').Replace('\', '/')
        $s = $rel
        if ($byName) { $s = $f.Name }
        # rg --files --glob=<pattern> without --hidden: a hidden FILE that matches the glob is listed,
        # a hidden DIRECTORY is only entered when the glob matches the directory itself.
        $skipHid = $false
        $segs = @($rel -split '/')
        for ($q = 0; $q -lt $segs.Count - 1; $q++) {
            if (-not $segs[$q].StartsWith('.')) { continue }
            $ds = $segs[$q]
            if (-not $byName) { $ds = (@($segs | Select-Object -First ($q + 1)) -join '/') }
            if (-not (Test-GlobMatch $rx $ds)) { $skipHid = $true; break }
        }
        if ($skipHid) { continue }
        if (Test-GlobMatch $rx $s) { $hits += $f }
    }
    Show-Sub (Get-Plural $hits.Count 'file')
    if ($hits.Count -eq 0) { return 'No files found' }
    # opencode lists them in ripgrep's own (unsorted, parallel-walk) order,
    # which cannot be reproduced; byte order of the path is the stable stand-in.
    $sorted = $hits
    if ($hits.Count -le 3000) { $sorted = @($hits | Sort-Object -Property @{ Expression = { -join ($_.FullName.ToCharArray() | ForEach-Object { '{0:x4}' -f [int]$_ }) } }) }
    $out = @()
    foreach ($f in ($sorted | Select-Object -First 100)) { $out += $f.FullName }
    if ($sorted.Count -ge 100) { $out += ''; $out += '(Results are truncated: showing first 100 results. Consider using a more specific path or pattern.)' }
    return ($out -join "`n")
}

function Tool-Grep($a) {
    $base = Get-SearchBase $a
    $pat = '' + (Get-Prop $a 'pattern')
    if (-not $pat) { throw 'pattern is empty' }
    try { $null = [regex]::Match('', $pat) }
    catch {
        $why = '' + $_.Exception.Message
        if ($_.Exception.InnerException) { $why = '' + $_.Exception.InnerException.Message }
        # Rust's wording for the commonest cases (the rest keep .NET's reason).
        if ($why -match 'Not enough \)') { $why = 'unclosed group' }
        elseif ($why -match 'Too many \)') { $why = 'unopened group' }
        elseif ($why -match 'Unterminated \[\]') { $why = 'unclosed character class' }
        throw ("rg: regex parse error:`n    (?:$pat)`n    ^`nerror: " + $why)
    }
    $inc = '' + (Get-Prop $a 'include')
    $files = Get-ProjectFiles $base
    if ($inc) {
        $rx = Convert-GlobToRegex $inc
        $byName = -not $inc.Replace('\', '/').Contains('/')
        $keep = @()
        foreach ($f in $files) {
            $s = $f.Name
            if (-not $byName) { $s = $f.FullName.Substring($base.TrimEnd('\', '/').Length).TrimStart('\', '/').Replace('\', '/') }
            if (Test-GlobMatch $rx $s) { $keep += $f }
        }
        $files = $keep
    }
    $files = @($files | Where-Object { $_.Length -lt 5000000 } | Sort-Object -Property LastWriteTime -Descending)
    $hits = @()
    if ($files.Count -gt 0) {
        $hits = @($files | Select-String -Pattern $pat -CaseSensitive -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -First 1000)
    }
    $inFiles = @($hits | ForEach-Object { $_.Path } | Sort-Object -Unique).Count
    Show-Sub ((Get-Plural $hits.Count 'match').Replace('matchs', 'matches') + ' in ' + (Get-Plural $inFiles 'file'))
    if ($hits.Count -eq 0) { return 'No files found' }
    $shown = @($hits | Select-Object -First 100)
    $more = ''
    if ($hits.Count -ge 100) { $more = ' (more matches available)' }
    $out = @("Found $($shown.Count) matches$more")
    $cur = ''
    $termCache = @{}
    foreach ($h in $shown) {
        if ($h.Path -ne $cur) { if ($cur) { $out += '' }; $cur = $h.Path; $out += ($h.Path + ':') }
        $l = $h.Line
        $cut = $false
        if ($l.Length -gt 2000) { $l = $l.Substring(0, 2000) + '...'; $cut = $true }
        # ripgrep's line text keeps its line terminator.
        # (the file's own terminator: CRLF stays CRLF, a last line without a newline gets none)
        if (-not $termCache.ContainsKey($h.Path)) { try { $termCache[$h.Path] = @((Read-TextFile $h.Path).text -split "`n") } catch { $termCache[$h.Path] = @() } }
        $fl = $termCache[$h.Path]
        $term = "`n"
        $ix = $h.LineNumber - 1
        if ($fl.Count -gt 0) { if ($ix -ge $fl.Count - 1) { $term = '' } elseif ($fl[$ix].EndsWith("`r")) { $term = "`r`n" } }
        if ($cut) { $term = '' }   # a truncated line has lost its terminator
        $out += "  Line $($h.LineNumber): $l" + $term
    }
    if ($hits.Count -ge 100) { $out += ''; $out += '(Results truncated. Consider using a more specific path or pattern.)' }
    return (Limit-Text ($out -join "`n"))
}

function Show-NewOutput([string]$file, [bool]$final, [string]$enc) {
    # Print command output lines that appeared since the last call (live tail).
    if (-not (Test-Path -LiteralPath $file)) { return }
    try {
        if ((Get-Item -LiteralPath $file).Length -gt 1000000) { return }
        if ($enc) { $txt = Get-Content -LiteralPath $file -Raw -Encoding $enc -ErrorAction Stop }
        else { $txt = Get-Content -LiteralPath $file -Raw -ErrorAction Stop }
    }
    catch { return }
    if (-not $txt) { return }
    $ls = @(($txt -replace '\x1b\[[0-9;?]*[A-Za-z]', '') -split "`r?`n")
    $complete = $ls.Count - 1
    if ($final) { $complete = $ls.Count; if ($ls[$ls.Count - 1] -eq '') { $complete-- } }
    for ($j = $script:LiveDone; $j -lt $complete; $j++) {
        if ($script:LiveShown -lt 12) { Show-More $ls[$j]; $script:LiveShown++ }
    }
    if ($complete -gt $script:LiveDone) { $script:LiveDone = $complete }
}

function Stop-Tree($proc) {
    $kids = @()
    if ($script:OnWindows) { try { $null = & taskkill.exe /T /F /PID $proc.Id 2>&1 } catch { } }
    else { try { $kids = @(& pgrep -P $proc.Id 2>$null) } catch { } }   # the shell first, then what it left running
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    foreach ($kid in $kids) { try { Stop-Process -Id ([int]$kid) -Force -ErrorAction SilentlyContinue } catch { } }
}

function Tool-Bash($a, [bool]$noAsk) {
    $cmd = ('' + (Get-Prop $a 'command')) -replace "`r`n", "`n"
    if (-not $cmd.Trim()) { throw 'command is empty' }
    $ms = $CommandTimeoutSec * 1000
    $t = Get-Prop $a 'timeout'
    if ($t) { try { $ms = [int]$t } catch { } }
    if ($ms -lt 1000) { $ms = 1000 }
    if ($ms -gt 600000) { $ms = 600000 }
    $wd = $script:RootFull
    $wdArg = Get-Prop $a 'workdir'
    if ($wdArg) {
        $wd = Get-FullPath $wdArg
        if (-not (Test-Path -LiteralPath $wd -PathType Container)) { throw "NotFound: FileSystem.access ($wd)" }
    }
    $cl = @(Get-Lines $cmd)
    if ($cl.Count -gt 1) {
        $k = 0
        foreach ($l in $cl) { if ($k -ge 15) { Show-More "$($script:G.bar) ... $($cl.Count - 15) more lines" 'DarkGray'; break }; Show-More ($script:G.bar + ' ' + $l) 'Yellow'; $k++ }
    }
    if ($wd -ne $script:RootFull) { Show-More "in $wd" }
    if (-not $noAsk) {
        $ap = Request-Approval 'Run it?'
        if (-not $ap.ok) { return (Get-DeclineText $ap) }
    }
    $id = Get-Random
    $outFile = Join-Path $script:TmpDir "cmd-$id.out"
    $errFile = Join-Path $script:TmpDir "cmd-$id.err"
    $inFile = Join-Path $script:TmpDir "cmd-$id.in"
    Set-Content -LiteralPath $inFile -Value '' -NoNewline
    $enc = ''
    if ($script:Shell.kind -eq 'bash') {
        $scriptFile = Join-Path $script:TmpDir "cmd-$id.sh"
        $pre = "exec 2>&1`n"
        # Off Windows pwsh relays redirected streams itself (it re-writes the output line by line, adding a
        # final newline, and feeds stdin through a pipe that fails with "Broken pipe" when the command exits
        # first), so there the script does its own redirection.
        if (-not $script:OnWindows) { $pre = "exec >'" + $outFile.Replace("'", "'\''") + "' 2>&1 </dev/null`n" }
        Write-TextFile $scriptFile ($pre + $cmd + "`n") $false
        $argList = '"' + $scriptFile.Replace('\', '/') + '"'
        $enc = 'UTF8'
    }
    else {
        $scriptFile = Join-Path $script:TmpDir "cmd-$id.ps1"
        # Stream each output line as it is made, no ANSI colour, and surface a
        # native program's exit code.
        $wrapped = "try { `$PSStyle.OutputRendering = 'PlainText' } catch { }`n& {`n" + $cmd + "`n} 2>&1 | Out-String -Stream -Width 200`nif (`$LASTEXITCODE) { exit `$LASTEXITCODE }`n"
        Set-Content -LiteralPath $scriptFile -Value $wrapped -Encoding UTF8
        $argList = '-NoProfile -NonInteractive -File "' + $scriptFile + '"'
    }
    $t0 = Get-Date
    $spArgs = @{ FilePath = $script:Shell.exe; ArgumentList = $argList; WorkingDirectory = $wd; NoNewWindow = $true; PassThru = $true }
    if ($script:OnWindows -or $script:Shell.kind -ne 'bash') {
        $spArgs['RedirectStandardOutput'] = $outFile; $spArgs['RedirectStandardError'] = $errFile; $spArgs['RedirectStandardInput'] = $inFile
    }
    $proc = Start-Process @spArgs
    $null = $proc.Handle
    $script:LiveDone = 0
    $script:LiveShown = 0
    $timedOut = $false
    $lastTick = -1
    $limit = [int]($ms / 1000)
    try {
      while (-not $proc.HasExited) {
        Start-Sleep -Milliseconds 200
        $el = [int]((Get-Date) - $t0).TotalSeconds
        if (((Get-Date) - $t0).TotalMilliseconds -ge $ms) { $timedOut = $true; Stop-Tree $proc; Start-Sleep -Milliseconds 500; break }
        Show-NewOutput $outFile $false $enc
        if ($el -ne $lastTick) { $lastTick = $el; Show-Wait "running $el s (timeout $limit s, Ctrl+C quits harness-lite)" }
      }
    }
    finally { if (-not $proc.HasExited) { Stop-Tree $proc } }   # interrupted (Esc / Ctrl+C): no orphan
    Clear-Wait
    Show-NewOutput $outFile $true $enc
    $secs = ((Get-Date) - $t0).TotalSeconds
    $stdout = ''
    $stderr = ''
    if (Test-Path -LiteralPath $outFile) { if ($enc) { $stdout = '' + (Get-Content -LiteralPath $outFile -Raw -Encoding UTF8) } else { $stdout = '' + (Get-Content -LiteralPath $outFile -Raw) } }
    if (Test-Path -LiteralPath $errFile) { $stderr = '' + (Get-Content -LiteralPath $errFile -Raw) }
    $stdout = $stdout -replace '\x1b\[[0-9;?]*[A-Za-z]', ''
    $stderr = $stderr -replace '\x1b\[[0-9;?]*[A-Za-z]', ''
    foreach ($f in @($scriptFile, $outFile, $errFile, $inFile)) { if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue } }
    if ($script:LiveDone -gt $script:LiveShown) { Show-More "... $($script:LiveDone - $script:LiveShown) more lines (the model sees all of it)" }
    if ($stderr.Trim()) { foreach ($l in @(Get-Lines $stderr.Trim() | Select-Object -Last 8)) { Show-More $l 'DarkYellow' } }
    $code = 0
    if (-not $timedOut) { try { $code = [int]$proc.ExitCode } catch { } }
    $status = "exit code $code"
    $color = 'Green'
    if ($code -ne 0) { $color = 'Red' }
    if ($timedOut) { $status = "killed after $limit s"; $color = 'Red' }
    Show-Sub ("$status $($script:G.mid) " + ('{0:N1}s' -f $secs)) $color
    # As opencode returns it: stdout then stderr, untrimmed.
    $res = $stdout
    if ($stderr) { $res += $stderr }
    # opencode keeps the last 2000 lines / 51200 bytes and saves the whole output to a file.
    $full = $res
    $rl = @($res -split "`n")
    $wasCut = $false
    if ($rl.Count -gt 2000 -or (Get-Utf8Len $res) -gt 51200) {
        $wasCut = $true
        $keep = @()
        $nb = 0
        for ($y = $rl.Count - 1; $y -ge 0 -and $keep.Count -lt 2000; $y--) {
            $u = Get-Utf8Len $rl[$y]
            if ($keep.Count -gt 0) { $u++ }
            if ($nb + $u -gt 51200) {
                if ($keep.Count -eq 0) { $one = $rl[$y]; if ($one.Length -gt 51200) { $one = $one.Substring($one.Length - 51200) }; $keep += $one }
                break
            }
            $keep += $rl[$y]
            $nb += $u
        }
        $ord = @()
        for ($y = $keep.Count - 1; $y -ge 0; $y--) { $ord += $keep[$y] }
        $res = $ord -join "`n"
    }
    if (-not $res) { $res = '(no output)' }
    if ($wasCut) {
        $saveDir = Join-Path $script:TmpDir 'tool-output'
        $saved = Join-Path $saveDir ('tool_' + (New-Guid).ToString().Replace('-', ''))
        try { Write-TextFile $saved $full $false } catch { }
        $res = "...output truncated...`n`nFull output saved to: $saved`n`n" + $res
    }
    if ($timedOut) { $res += "`n`n<shell_metadata>`nshell tool terminated command after exceeding timeout $ms ms. If this command is expected to take longer and is not waiting for interactive input, retry with a larger timeout value in milliseconds.`n</shell_metadata>" }
    return $res
}

function Convert-HtmlToText([string]$h, [bool]$md) {
    $h = $h -replace '(?is)<(script|style|noscript|svg|head|template)\b.*?</\1\s*>', ' '
    $h = $h -replace '(?is)<!--.*?-->', ' '
    if ($md) {
        $h = $h -replace '(?is)<h1[^>]*>(.*?)</h1\s*>', "`n# `$1`n"
        $h = $h -replace '(?is)<h2[^>]*>(.*?)</h2\s*>', "`n## `$1`n"
        $h = $h -replace '(?is)<h[3-6][^>]*>(.*?)</h[3-6]\s*>', "`n### `$1`n"
        $h = $h -replace '(?is)<li[^>]*>', "`n- "
        $h = $h -replace '(?is)<a\s[^>]*?href\s*=\s*"([^"]*)"[^>]*>(.*?)</a\s*>', '[$2]($1)'
        $h = $h -replace '(?is)<(pre)[^>]*>', "`n``````n"
        $h = $h -replace '(?is)</pre\s*>', "`n``````n"
        $h = $h -replace '(?is)<code[^>]*>(.*?)</code\s*>', '`$1`'
    }
    $h = $h -replace '(?i)<br\s*/?>', "`n"
    $h = $h -replace '(?i)</?(p|div|section|article|header|footer|main|nav|aside|tr|ul|ol|table|blockquote|h[1-6]|li|dt|dd)\b[^>]*>', "`n"
    $h = $h -replace '<[^>]+>', ''
    $h = $h.Replace('&nbsp;', ' ').Replace('&lt;', '<').Replace('&gt;', '>').Replace('&quot;', '"').Replace('&#39;', "'").Replace('&#x27;', "'").Replace('&amp;', '&')
    $lines = @()
    $blank = $false
    foreach ($l in ($h -split "`r?`n")) {
        $t = ($l -replace '[ \t]+', ' ').Trim()
        if (-not $t) { if (-not $blank -and $lines.Count -gt 0) { $lines += '' }; $blank = $true; continue }
        $blank = $false
        $lines += $t
    }
    return ($lines -join "`n").Trim()
}

function Convert-HtmlToPlain([string]$h) {
    # opencode: the text nodes outside script/style/noscript/iframe/object/embed, concatenated, trimmed.
    $h = $h -replace '(?is)<(script|style|noscript|iframe|object|embed)\b.*?</\1\s*>', ''
    $h = $h -replace '(?is)<!--.*?-->', ''
    $h = $h -replace '(?s)<[^>]+>', ''
    return $h.Trim()
}

function Convert-HtmlToMarkdown([string]$h) {
    # An approximation of turndown as opencode sets it up (atx headings, "-" bullets, fenced code, * emphasis).
    $h = $h -replace '(?is)<(script|style|meta|link|noscript|iframe|object|embed)\b.*?</\1\s*>', ''
    $h = $h -replace '(?is)<(meta|link)\b[^>]*>', ''
    $h = $h -replace '(?is)<!--.*?-->', ''
    $h = $h -replace '(?is)<title[^>]*>(.*?)</title\s*>', "`$1`n`n"
    foreach ($n in 1..6) { $h = $h -replace "(?is)<h$n\b[^>]*>(.*?)</h$n\s*>", ("`n`n" + ('#' * $n) + " `$1`n`n") }
    $h = $h -replace '(?is)<(b|strong)\b[^>]*>(.*?)</\1\s*>', '**$2**'
    $h = $h -replace '(?is)<(i|em)\b[^>]*>(.*?)</\1\s*>', '*$2*'
    $h = $h -replace '(?is)<a\s[^>]*?href\s*=\s*"([^"]*)"[^>]*>(.*?)</a\s*>', '[$2]($1)'
    $h = $h -replace '(?is)<pre[^>]*>\s*(?:<code[^>]*>)?(.*?)(?:</code\s*>)?\s*</pre\s*>', "`n`n```````n`$1`n```````n`n"
    $h = $h -replace '(?is)<code[^>]*>(.*?)</code\s*>', '`$1`'
    $h = $h -replace '(?is)<li\b[^>]*>(.*?)</li\s*>', "`n-   `$1"
    $h = $h -replace '(?i)<hr\s*/?>', "`n`n---`n`n"
    $h = $h -replace '(?i)<br\s*/?>', "  `n"
    $h = $h -replace '(?i)</?(p|div|section|article|header|footer|main|nav|aside|tr|ul|ol|table|blockquote|dt|dd)\b[^>]*>', "`n`n"
    $h = $h -replace '(?s)<[^>]+>', ''
    $h = $h.Replace('&nbsp;', ' ').Replace('&lt;', '<').Replace('&gt;', '>').Replace('&quot;', '"').Replace('&#39;', "'").Replace('&#x27;', "'").Replace('&amp;', '&')
    $h = $h -replace "`r`n", "`n"
    $h = $h -replace "`n[ \t]+`n", "`n`n"
    $h = $h -replace "`n{3,}", "`n`n"
    return $h.Trim()
}

function Tool-WebFetch($a) {
    $url = '' + (Get-Prop $a 'url')
    if ($url -notmatch '^https?://') { throw 'URL must start with http:// or https://' }
    $fmt = '' + (Get-Prop $a 'format')
    if (-not $fmt) { $fmt = 'markdown' }
    $sec = 30
    if (Get-Prop $a 'timeout') { $sec = [int](Get-Prop $a 'timeout') }
    if ($sec -gt 120) { $sec = 120 }
    if ($sec -lt 1) { $sec = 30 }
    $hdr = @{ 'User-Agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
        'Accept' = 'text/html,application/xhtml+xml,text/plain,text/markdown,*/*;q=0.8' }
    try { $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $sec -Headers $hdr }
    catch {
        $sc = 0
        try { $sc = [int]$_.Exception.Response.StatusCode } catch { }
        if ($sc -gt 0) { throw "StatusCode: non 2xx status code ($sc GET $url)" }
        throw "Transport error (GET $url)"
    }
    $body = $r.Content
    if (-not ($body -is [string])) { throw "the response is binary ($($r.Headers['Content-Type'])), not text" }
    $ctype = '' + @($r.Headers['Content-Type'])[0]
    $isHtml = ($ctype -match 'html') -or ($body -match '(?i)^\s*(<!doctype html|<html)')
    $out = $body
    $isHtml = ($ctype -match 'text/html')
    if ($isHtml -and $fmt -eq 'markdown') { $out = Convert-HtmlToMarkdown $body }
    elseif ($isHtml -and $fmt -eq 'text') { $out = Convert-HtmlToPlain $body }
    Show-Sub "$($body.Length) chars, $ctype"
    return (Limit-Text $out)
}

$script:TodoMarks = @{ completed = 'x'; in_progress = '~'; cancelled = '-'; pending = ' ' }

function Tool-TodoWrite($a) {
    $todos = Get-Prop $a 'todos'
    if ($todos -is [string]) { $todos = ConvertFrom-ArgsJson $todos }
    if ($null -eq $todos) { throw 'todos is required' }
    $list = @($todos)
    foreach ($t in $list) {
        if ($null -eq $t -or $t -is [string]) { throw 'each todo must be an object with content, status and priority' }
        if (-not ('' + (Get-Prop $t 'content')).Trim()) { throw 'each todo needs a non-empty content' }
    }
    $script:LastTodos = $list
    $open = 0
    foreach ($t in $list) {
        $st = ('' + (Get-Prop $t 'status')).Trim().ToLower()
        if (-not $script:TodoMarks.ContainsKey($st)) { $st = 'pending' }
        if ($st -ne 'completed' -and $st -ne 'cancelled') { $open++ }
        $c = 'Gray'
        if ($st -eq 'completed' -or $st -eq 'cancelled') { $c = 'DarkGray' } elseif ($st -eq 'in_progress') { $c = 'Yellow' }
        Show-More ("[" + $script:TodoMarks[$st] + "] " + (('' + (Get-Prop $t 'content')) -replace '\s+', ' ').Trim()) $c
    }
    # JSON.stringify(todos, null, 2), keys in opencode's order.
    if ($list.Count -eq 0) { return '[]' }
    $items = @()
    foreach ($t in $list) {
        $items += "  {`n    `"content`": " + (ConvertTo-JsonText ('' + (Get-Prop $t 'content')) $false) + ",`n    `"status`": " + (ConvertTo-JsonText ('' + (Get-Prop $t 'status')) $false) +
        ",`n    `"priority`": " + (ConvertTo-JsonText ('' + (Get-Prop $t 'priority')) $false) + "`n  }"
    }
    return "[`n" + ($items -join ",`n") + "`n]"
}

# opencode's parameter schemas: key, type, required (in schema order).
$script:ToolSchema = @{
    bash      = @(@('command', 'string', $true), @('timeout', 'number', $false), @('workdir', 'string', $false), @('description', 'string', $false))
    read      = @(@('filePath', 'string', $true), @('offset', 'number', $false), @('limit', 'number', $false))
    edit      = @(@('filePath', 'string', $true), @('oldString', 'string', $true), @('newString', 'string', $true), @('replaceAll', 'boolean', $false))
    write     = @(@('content', 'string', $true), @('filePath', 'string', $true))
    glob      = @(@('pattern', 'string', $true), @('path', 'string', $false))
    grep      = @(@('pattern', 'string', $true), @('path', 'string', $false), @('include', 'string', $false))
    webfetch  = @(@('url', 'string', $true), @('format', 'text|markdown|html', $false), @('timeout', 'number', $false))
    todowrite = @(, @('todos', 'any', $true))
    skill     = @(, @('name', 'string', $true))
    task      = @(@('description', 'string', $true), @('prompt', 'string', $true), @('subagent_type', 'string', $true), @('task_id', 'string', $false), @('command', 'string', $false))
    websearch = @(, @('query', 'string', $true))
}

$script:ToolLabels = @{ bash = 'Bash'; read = 'Read'; edit = 'Edit'; write = 'Write'; glob = 'Glob'; grep = 'Grep'; todowrite = 'Todo'; webfetch = 'Fetch'
    websearch = 'Search'; skill = 'Skill'; task = 'Task'; apply_patch = 'Patch' }

function Invoke-Tool([string]$name, $a, [bool]$noAsk) {
    $req = $script:A.Req
    if (-not $req.ContainsKey($name)) {
        throw "Model tried to call unavailable tool '$name'. Available tools: $((@(@($req.Keys) + 'invalid') | Sort-Object) -join ', ')."
    }
    $bad = ''
    if ($null -eq $a -or $a -is [string] -or $a -is [array]) { $bad = 'Expected object, got ' + (ConvertTo-PyJson $a $false $false $true) }
    elseif ($script:ToolSchema.ContainsKey($name)) {
        # opencode's schema check: keys in schema order, first problem reported.
        foreach ($f in $script:ToolSchema[$name]) {
            $k = $f[0]; $ty = $f[1]; $need = $f[2]
            if (-not (Test-Prop $a $k)) {
                if ($need) { $bad = "Missing key`n  at [`"$k`"]"; break }
                continue
            }
            if ($a -is [hashtable]) { $v = $a[$k] } else { $v = $a.PSObject.Properties[$k].Value }   # (Get-Prop would unroll a one-item array)
            $ok = $true
            $exp = $ty
            if ($ty -eq 'string') { $ok = ($v -is [string]) }
            elseif ($ty -eq 'number') { $ok = ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal]) }
            elseif ($ty -eq 'boolean') { $ok = ($v -is [bool]) }
            elseif ($ty -eq 'any') { $ok = $true }
            else {
                $opts = @($ty -split '\|')
                $ok = ($v -is [string]) -and ($opts -ccontains $v)
                $exp = ($opts | ForEach-Object { '"' + $_ + '"' }) -join ' | '
            }
            if (-not $ok) {
                if (-not $need -and ($ty -eq 'string' -or $ty -eq 'number' -or $ty -eq 'boolean')) { $exp += ' | undefined' }
                $bad = "Expected $exp, got " + (ConvertTo-PyJson $v $false $false $true) + "`n  at [`"$k`"]"
                break
            }
        }
    }
    else {
        foreach ($p in $req[$name]) { if (-not (Test-Prop $a $p)) { $bad = "Missing key`n  at [`"$p`"]"; break } }
    }
    if ($bad) { throw "The $name tool was called with invalid arguments: SchemaError($bad).`nPlease rewrite the input so it satisfies the expected schema." }
    switch ($name) {
        'bash' { return Tool-Bash $a $noAsk }
        'read' { return Tool-Read $a }
        'edit' { return Tool-Edit $a }
        'write' { return Tool-Write $a }
        'glob' { return Tool-Glob $a }
        'grep' { return Tool-Grep $a }
        'todowrite' { return Tool-TodoWrite $a }
        'webfetch' { return Tool-WebFetch $a }
        'websearch' { return Tool-WebSearch $a }
        'skill' { return Tool-Skill $a }
        'task' { return Tool-Task $a }
        'apply_patch' { return Tool-ApplyPatch $a }
    }
}

function Get-ToolDetail([string]$name, $a) {
    if ($null -eq $a -or $a -is [string]) { return '' }
    $d = ''
    switch ($name) {
        'bash' { $d = @(Get-Lines ('' + (Get-Prop $a 'command')))[0]; $desc = '' + (Get-Prop $a 'description'); if ($desc) { $d = "$d  # $desc" } }
        'read' { $d = Get-RelPath (Get-FullPath (Get-Prop $a 'filePath')); if (Get-Prop $a 'offset') { $d += " (from line $(Get-Prop $a 'offset'))" } }
        'glob' { $d = '' + (Get-Prop $a 'pattern'); if (Get-Prop $a 'path') { $d += " in $(Get-Prop $a 'path')" } }
        'grep' { $d = '"' + (Get-Prop $a 'pattern') + '"'; if (Get-Prop $a 'include') { $d += " in $(Get-Prop $a 'include')" }; if (Get-Prop $a 'path') { $d += " under $(Get-Prop $a 'path')" } }
        'todowrite' { $d = Get-Plural @(Get-Prop $a 'todos').Count 'item' }
        'webfetch' { $d = '' + (Get-Prop $a 'url') }
        'websearch' { $d = '' + (Get-Prop $a 'query') }
        'skill' { $d = '' + (Get-Prop $a 'name') }
        'task' { $d = ('' + (Get-Prop $a 'subagent_type')) + ': ' + (Get-Prop $a 'description') }
        'apply_patch' { $d = (@(Get-Lines ('' + (Get-Prop $a 'patchText'))) | Where-Object { $_ -match '^\*\*\* (Add|Update|Delete) File: ' } | ForEach-Object { $_.Substring(4) }) -join ', ' }
        default { $fp = Get-Prop $a 'filePath'; if ($fp) { $d = Get-RelPath (Get-FullPath $fp) } }
    }
    return $d
}

function Show-ToolCall([string]$name, $a) {
    $label = $script:ToolLabels[$name]
    if (-not $label) { $label = $name }
    $color = 'Green'
    if ($name -eq 'bash') { $color = 'Yellow' }
    if ($script:A.Sub) { W ('  ' + $script:G.bar + ' ') 'DarkGray' }
    W ($script:G.dot + ' ') $color
    W $label $color
    WL (Limit-Line ('  ' + (Get-ToolDetail $name $a)) ((Get-Width) - $label.Length - 3))
}

# ---------------------------------------------------------------- system prompt and environment
# What opencode (as harness host runs it) puts in the system message: the
# model family's prompt, the <env> block, every instruction file, and the
# skills list; the proxy then wraps it and adds its tool guidance.
function Find-Up([string]$dir, [string]$name) {
    $d = $dir
    while ($d) {
        $p = Join-Path $d $name
        if (Test-Path -LiteralPath $p) { return $p }
        $parent = Split-Path -Parent $d
        if (-not $parent -or $parent -eq $d) { break }
        $d = $parent
    }
    return ''
}

function Get-UpDirs {
    # The folder, then each parent up to the workspace root (opencode's findUp range).
    $dirs = @()
    $d = $script:RootFull
    while ($d) {
        $dirs += $d
        if ($script:Worktree -ne '/' -and $d -eq $script:Worktree) { break }
        $parent = Split-Path -Parent $d
        if (-not $parent -or $parent -eq $d) { break }
        $d = $parent
    }
    return $dirs
}

function Get-FamilyHead([string]$id) {
    # opencode's provider prompt choice, in its order.
    if ($id.Contains('muse')) {
        $n = 'Muse Spark'
        if ($id.Contains('muse-glimmer')) { $n = 'Muse Glimmer' }
        return $script:TX.P_muse.Replace('{{MODEL_NAME}}', $n)
    }
    if ($id.Contains('gpt-4') -or $id.Contains('o1') -or $id.Contains('o3')) { return $script:TX.P_gpt4 }
    if ($id.Contains('gpt')) {
        if ($id.Contains('gpt-6')) { return $script:TX.P_gpt6 }
        if ($id.Contains('codex')) { return $script:TX.P_codex }
        return $script:TX.P_gpt
    }
    if ($id.Contains('gemini-')) { return $script:TX.P_gemini }
    if ($id.Contains('claude')) { return $script:TX.P_claude }
    $l = $id.ToLower()
    if ($l.Contains('trinity')) { return $script:TX.P_trinity }
    if ($l.Contains('kimi')) { return $script:TX.P_kimi }
    return $script:TX.P_default
}

function Test-PatchModel([string]$id) {
    # opencode swaps edit/write for apply_patch on these ids.
    return ($id.Contains('gpt-') -and -not $id.Contains('oss') -and -not $id.Contains('gpt-4'))
}

function Get-OcDate {
    # JavaScript's Date.toDateString(), independent of the Windows locale.
    $d = Get-Date
    $days = @('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')
    $months = @('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    return $days[[int]$d.DayOfWeek] + ' ' + $months[$d.Month - 1] + ' ' + ('{0:D2}' -f $d.Day) + ' ' + $d.Year
}

function Get-InstructionFiles {
    $files = @()
    foreach ($g in @((Join-Path $script:OcConfig 'AGENTS.md'), (Join-Path (Join-Path $script:HomeDir '.claude') 'CLAUDE.md'))) {
        if (Test-Path -LiteralPath $g -PathType Leaf) { $files += $g; break }
    }
    foreach ($name in @('AGENTS.md', 'CLAUDE.md', 'CONTEXT.md')) {
        $found = @()
        foreach ($d in (Get-UpDirs)) {
            $p = Join-Path $d $name
            if ((Test-Path -LiteralPath $p -PathType Leaf) -and -not ($files -contains $p)) { $found += $p }
        }
        if ($found.Count -gt 0) { $files += $found; break }
    }
    return $files
}

function ConvertTo-XmlText([string]$s) {
    return $s.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;').Replace('"', '&quot;').Replace("'", '&#39;')
}

function Read-SkillFile([string]$path) {
    $t = (Read-TextFile $path).text.Replace("`r`n", "`n")
    $name = ''
    $desc = ''
    $body = $t
    if ($t.StartsWith("---`n")) {
        $end = Find-Str $t "`n---" 4
        if ($end -ge 0) {
            $fold = $false
            foreach ($line in ($t.Substring(4, $end - 4) -split "`n")) {
                if ($fold -and $line -match '^\s+\S') { $desc = ($desc + ' ' + $line.Trim()).Trim(); continue }
                $fold = $false
                if ($line -match '^name:\s*(.*)$') { $name = $Matches[1].Trim().Trim('"').Trim("'") }
                elseif ($line -match '^description:\s*(.*)$') {
                    $desc = $Matches[1].Trim().Trim('"').Trim("'")
                    if ($desc -match '^[>|][-+]?$') { $desc = ''; $fold = $true }
                }
            }
            $nl = Find-Str $t "`n" ($end + 4)
            if ($nl -lt 0) { $body = '' } else { $body = $t.Substring($nl + 1) }
        }
    }
    if (-not $name) { $name = Split-Path -Leaf (Split-Path -Parent $path) }
    return @{ name = $name; desc = $desc; location = $path; dir = (Split-Path -Parent $path); body = $body }
}

function Get-Skills {
    # opencode's skill discovery: ~/.claude/skills, ~/.agents/skills, the same
    # folders in the project (up to the workspace root), .opencode/skill(s).
    $roots = @((Join-Path (Join-Path $script:HomeDir '.claude') 'skills'), (Join-Path (Join-Path $script:HomeDir '.agents') 'skills'),
        (Join-Path $script:OcConfig 'skill'), (Join-Path $script:OcConfig 'skills'))
    foreach ($d in (Get-UpDirs)) {
        foreach ($sub in @('.claude', '.agents')) { $roots += (Join-Path (Join-Path $d $sub) 'skills') }
        foreach ($sub in @('skill', 'skills')) { $roots += (Join-Path (Join-Path $d '.opencode') $sub) }
    }
    $byName = @{}
    foreach ($r in $roots) {
        if (-not (Test-Path -LiteralPath $r -PathType Container)) { continue }
        foreach ($f in @(Get-ChildItem -LiteralPath $r -Recurse -Filter 'SKILL.md' -File -ErrorAction SilentlyContinue)) {
            try { $s = Read-SkillFile $f.FullName } catch { continue }
            $byName[$s.name] = $s   # opencode: the last one found wins
        }
    }
    if (-not $byName.ContainsKey('customize-opencode')) {
        $byName['customize-opencode'] = @{ name = 'customize-opencode'; desc = $script:TX.SkillBuiltinDesc; location = '<built-in>'; dir = '.'; body = $script:TX.SkillBuiltin }
    }
    $out = @()
    foreach ($k in @($byName.Keys | Sort-Object)) { $out += $byName[$k] }
    return $out
}

function Format-SkillsBlock {
    $s = "Skills provide specialized instructions and workflows for specific tasks.`nUse the skill tool to load a skill when a task matches its description.`n<available_skills>"
    foreach ($k in $script:Skills) {
        $s += "`n  <skill>`n    <name>$($k.name)</name>`n    <description>$($k.desc)</description>`n    <location>$(ConvertTo-XmlText $k.location)</location>`n  </skill>"
    }
    return $s + "`n</available_skills>"
}

$script:TextsReady = $false

function Initialize-Environment {
    if (-not $script:TextsReady) {
        # opencode's dynamic bits in its tool descriptions.
        $plat = 'linux'
        if ($script:OnWindows) { $plat = 'win32' } elseif ($IsMacOS) { $plat = 'darwin' }
        $script:Platform = $plat
        $shellName = 'bash'
        if ($script:Shell.kind -ne 'bash') { $shellName = 'powershell' }
        foreach ($k in @($script:TX.Keys)) {
            if (-not $k.StartsWith('SA_')) { continue }
            $script:TX[$k] = $script:TX[$k].Replace('{{OC_OS}}', $plat).Replace('{{OC_SHELL}}', $shellName).Replace('{{OC_TMP}}', $script:OcTmp).Replace('{{YEAR}}', '' + (Get-Date).Year)
        }
        $script:ReqAll = @{}
        $rq = $script:TX.ToolRequired | ConvertFrom-Json
        foreach ($p in $rq.PSObject.Properties) { $script:ReqAll[$p.Name] = @($p.Value) }
        $script:TextsReady = $true
    }
    $gitDir = Find-Up $script:RootFull '.git'
    $script:Worktree = '/'
    if ($gitDir) { $script:Worktree = Split-Path -Parent $gitDir }
    $git = 'no'
    if ($gitDir) { $git = 'yes' }
    $script:InstrFiles = @(Get-InstructionFiles)
    $script:InstrText = ''
    foreach ($f in $script:InstrFiles) {
        try { $script:InstrText += "`nInstructions from: $f`n" + (Read-TextFile $f).text } catch { }
    }
    $script:Skills = @(Get-Skills)
    $script:SkillsBlock = Format-SkillsBlock
    $script:EnvBlockTpl = "`nYou are powered by the model named @@ID@@. The exact model ID is harness/@@ID@@`n" +
    "Here is some useful information about the environment you are running in:`n<env>`n" +
    "  Working directory: $($script:RootFull)`n  Workspace root folder: $($script:Worktree)`n  Is directory a git repo: $git`n" +
    "  Platform: $($script:Platform)`n  Today's date: $(Get-OcDate)`n</env>"
    # The reminder's Environment bullet, as the proxy writes it on a host install.
    $os = 'linux'
    if ($script:OnWindows) { $os = 'windows' } elseif ($IsMacOS) { $os = 'macos' }
    $hostOs = " (host OS: $os)"
    $envText = $script:TX.EnvHead + $hostOs + $script:TX.EnvTail
    if ($script:OnWindows -and $script:Shell.kind -eq 'bash') { $envText += $script:TX.GitBashNote }
    if ($script:Shell.kind -ne 'bash') {
        $envText += " Your ``bash`` tool runs Windows PowerShell $($PSVersionTable.PSVersion) here, not bash (Git Bash is not available on this machine): write PowerShell commands, and do not use bash syntax."
    }
    $cwd = " The working directory for this turn is ``$($script:RootFull)`` " + [string][char]0x2014 + " when the user says `"this folder`", `"here`", `"my machine`", or `"the workspace`", they mean exactly that path."
    $instrList = @($script:InstrFiles | Where-Object { $_.Length -le 400 -and $_ -notmatch '[\x00-\x1f`]' -and $_ -notmatch '^https?://' } | Select-Object -First 6)
    if ($instrList.Count -gt 0) {
        $cwd += $script:TX.InstrClause.Replace('`@@A@@`, `@@B@@`', (($instrList | ForEach-Object { '`' + $_ + '`' }) -join ', '))
    }
    $script:Expand = @{ '{{HOST_OS}}' = $hostOs; '{{CWD}}' = $cwd; '{{ENVIRONMENT}}' = $envText; '{{TOOL_ENTRIES}}' = ''; '{{INSTRUCTIONS}}' = '' }
    $script:A = New-AgentCtx 'main' $script:ModelId
}

function New-AgentCtx([string]$kind, [string]$modelId) {
    # One agent's request shape: main (the user's agent), general or explore
    # (task subagents). Model and mode follow the proxy's slim rules.
    $mode = Get-ModeFor $modelId
    $ocId = $modelId   # the id opencode names in <env>, even when the proxy reroutes
    if ($kind -eq 'explore' -and $mode -eq 'native') {
        $free = Get-FreeCounterpart $modelId
        if ($free) { $modelId = $free; $mode = 'prompt' }
    }
    $set = $kind
    if ($kind -ne 'explore' -and (Test-PatchModel $modelId)) { $set = $kind + '_gpt' }
    $head = $script:TX.P_explore
    if ($kind -ne 'explore') { $head = Get-FamilyHead $modelId }
    $tail = $script:EnvBlockTpl.Replace('@@ID@@', $ocId) + $script:InstrText
    if ($kind -ne 'explore') { $tail += "`n" + $script:SkillsBlock }
    $nsys = $head + $tail
    if ($kind -eq 'main') { $nsys = $script:TX.SlimMain + $tail } elseif ($kind -eq 'general') { $nsys = $script:TX.SlimGeneral + $tail }
    $names = @{ finish = 1 }
    $req = @{}
    foreach ($n in ($script:TX['NAMES_' + $set] -split ' ')) { $names[$n] = 1; $req[$n] = $script:ReqAll[$n] }
    return @{ Kind = $kind; Model = $modelId; Mode = $mode; System = $head + $tail; NSystem = $nsys
        SA = $script:TX['SA_' + $set]; TE = $script:TX['TE_' + $set]; NT = $script:TX['NT_' + $set]
        Names = $names; Req = $req; HasTodo = $req.ContainsKey('todowrite'); Sub = ($kind -ne 'main') }
}

function Get-FreeCounterpart([string]$id) {
    if ($id -notmatch '-withtools$') { return '' }
    $base = $id.Substring(0, $id.Length - 10)
    if (-not $script:Catalog.ContainsKey($base)) { return '' }
    if ((Get-ModeFor $base) -eq 'native') { return '' }
    if (Test-FreeModel $base) { return $base }
    return ''
}

function Test-FreeModel([string]$id) {
    $info = $script:Catalog[$id]
    return (($info -and ($info.lane -eq 'assist' -or $info.w -eq '0/0')) -or $id -match '-assist$' -or $id -eq 'gemini-enterprise')
}

# ---------------------------------------------------------------- skill, websearch, task
function Tool-Skill($a) {
    $name = ('' + (Get-Prop $a 'name')).Trim()
    $s = $null
    foreach ($k in $script:Skills) { if ($k.name -eq $name) { $s = $k; break } }
    if ($null -eq $s) { throw "Skill `"$name`" not found. Available skills: $((@($script:Skills | ForEach-Object { $_.name })) -join ', ')" }
    $base = $s.dir
    $files = @()
    $scan = $s.dir
    if ($s.location -eq '<built-in>') { $scan = $script:RootFull }
    try {
        foreach ($f in @(Get-ChildItem -LiteralPath $scan -Recurse -File -Force -ErrorAction SilentlyContinue | Select-Object -First 50)) {
            if ($f.Name -eq 'SKILL.md') { continue }
            $files += "<file>$($f.FullName)</file>"
            if ($files.Count -ge 10) { break }
        }
    }
    catch { }
    Show-Sub "$name loaded"
    return "<skill_content name=`"$name`">`n# Skill: $name`n`n$($s.body.Trim())`n`nBase directory for this skill: $base`n" +
    "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.`nNote: file list is sampled.`n`n<skill_files>`n" +
    ($files -join "`n") + "`n</skill_files>`n</skill_content>"
}

function Tool-WebSearch($a) {
    # opencode's websearch: Exa's hosted MCP endpoint, no key needed.
    $q = '' + (Get-Prop $a 'query')
    $typ = 'auto'; if (Get-Prop $a 'type') { $typ = '' + (Get-Prop $a 'type') }
    $num = 8; if (Get-Prop $a 'numResults') { $num = [int](Get-Prop $a 'numResults') }
    $lc = 'fallback'; if (Get-Prop $a 'livecrawl') { $lc = '' + (Get-Prop $a 'livecrawl') }
    $argJson = '"query":' + (ConvertTo-JsonText $q $false) + ',"type":' + (ConvertTo-JsonText $typ $false) + ',"numResults":' + $num + ',"livecrawl":' + (ConvertTo-JsonText $lc $false)
    if (Get-Prop $a 'contextMaxCharacters') { $argJson += ',"contextMaxCharacters":' + [int](Get-Prop $a 'contextMaxCharacters') }
    $body = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"web_search_exa","arguments":{' + $argJson + '}}}'
    $url = 'https://mcp.exa.ai/mcp'
    if ($env:EXA_API_KEY) { $url += '?exaApiKey=' + $env:EXA_API_KEY }
    $r = Invoke-WebRequest -Uri $url -Method POST -UseBasicParsing -TimeoutSec 25 -Body (ConvertTo-AsciiJson $body) -ContentType 'application/json' -Headers @{ Accept = 'application/json, text/event-stream' }
    $text = '' + $r.Content
    $docs = @()
    if ($text.TrimStart().StartsWith('{')) { $docs += $text }
    else { foreach ($line in ($text -split "`n")) { if ($line.StartsWith('data: ')) { $docs += $line.Substring(6) } } }
    foreach ($d in $docs) {
        try { $j = $d | ConvertFrom-Json } catch { continue }
        if ($j.result -and $j.result.content) {
            foreach ($c in @($j.result.content)) { if ($c.text) { Show-Sub "$($c.text.Length) chars"; return ('' + $c.text) } }
        }
    }
    return 'No search results found. Please try a different query.'
}

$script:Tasks = @{}

function Tool-Task($a) {
    $type = ('' + (Get-Prop $a 'subagent_type')).Trim()
    if ($type -ne 'explore' -and $type -ne 'general') { throw "Unknown agent type: $type is not a valid agent type" }
    $tid = ('' + (Get-Prop $a 'task_id')).Trim()
    $sub = $null
    if ($tid -and $script:Tasks.ContainsKey($tid)) { $sub = $script:Tasks[$tid] }
    if ($null -eq $sub) {
        $tid = 'ses_' + (New-Guid).ToString().Replace('-', '').Substring(0, 12) + (New-Guid).ToString().Replace('-', '').Substring(0, 14)
        $sub = @{ kind = $type; msgs = @(); tag = (New-Guid).ToString().Replace('-', '').Substring(0, 12) }
    }
    $saved = @{ A = $script:A; M = $script:Messages; T = $script:LastTodos; Tag = $script:SessionTag }
    $r = $null
    try {
        $script:A = New-AgentCtx $sub.kind $script:ModelId
        $script:Messages = @($sub.msgs) + @(@{ role = 'user'; content = '' + (Get-Prop $a 'prompt') })
        $script:LastTodos = $null
        $script:SessionTag = $sub.tag
        Show-Sub "$($sub.kind) subagent $($script:G.mid) $($script:A.Model)"
        $r = Invoke-AgentLoop $MaxSteps
        $sub.msgs = $script:Messages
        $script:Tasks[$tid] = $sub
    }
    finally {
        $script:A = $saved.A; $script:Messages = $saved.M; $script:LastTodos = $saved.T; $script:SessionTag = $saved.Tag
    }
    return "<task id=`"$tid`" state=`"completed`">`n<task_result>`n$($r.text)`n</task_result>`n</task>"
}

function Tool-ApplyPatch($a) {
    # opencode's apply_patch (gpt models): *** Begin Patch / Add|Update|Delete File / *** End Patch.
    $lines = @((('' + (Get-Prop $a 'patchText')) -replace "`r`n", "`n").Split("`n"))
    $i = 0
    while ($i -lt $lines.Count -and $lines[$i].Trim() -ne '*** Begin Patch') { $i++ }
    if ($i -ge $lines.Count) { throw 'apply_patch verification failed: the patch must start with *** Begin Patch' }
    $i++
    $ops = @()
    while ($i -lt $lines.Count) {
        $l = $lines[$i]
        if ($l.Trim() -eq '*** End Patch') { break }
        if ($l -match '^\*\*\* (Add|Delete|Update) File: (.+)$') {
            $op = @{ kind = $Matches[1]; path = $Matches[2].Trim(); body = @(); move = '' }
            $i++
            while ($i -lt $lines.Count -and -not ($lines[$i] -match '^\*\*\* (Add File|Delete File|Update File|End Patch)')) {
                if ($lines[$i] -match '^\*\*\* Move to: (.+)$') { $op.move = $Matches[1].Trim() } else { $op.body += $lines[$i] }
                $i++
            }
            $ops += $op
            continue
        }
        $i++
    }
    if ($ops.Count -eq 0) { throw 'apply_patch verification failed: no file operations found in the patch' }
    $plan = @()
    foreach ($op in $ops) {
        $full = Resolve-ToolPath $op.path
        if ($op.kind -eq 'Add') {
            $txt = (@($op.body | Where-Object { $_.StartsWith('+') } | ForEach-Object { $_.Substring(1) }) -join "`n")
            if ($op.body.Count -gt 0) { $txt += "`n" }
            $plan += @{ kind = 'A'; full = $full; text = $txt; bom = $false }
            continue
        }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "apply_patch verification failed: Failed to read file to update: $full" }
        if ($op.kind -eq 'Delete') { $plan += @{ kind = 'D'; full = $full }; continue }
        $f = Read-TextFile $full
        $cur = @(($f.text -replace "`r`n", "`n").Split("`n"))
        $pos = 0
        $chunk = @()
        $chunks = @()
        foreach ($b in $op.body) {
            if ($b.StartsWith('@@')) { if ($chunk.Count -gt 0) { $chunks += , $chunk }; $chunk = @(); continue }
            if ($b -eq '*** End of File') { continue }
            $chunk += $b
        }
        if ($chunk.Count -gt 0) { $chunks += , $chunk }
        foreach ($c in $chunks) {
            $old = @(); $new = @()
            foreach ($b in $c) {
                $t = ' '; $v = $b
                if ($b.Length -gt 0) { $t = $b.Substring(0, 1); $v = $b.Substring(1) }
                if ($t -eq '-') { $old += $v } elseif ($t -eq '+') { $new += $v } else { $old += $v; $new += $v }
            }
            $at = -1
            foreach ($trim in @($false, $true)) {
                for ($k = $pos; $k -le $cur.Count - $old.Count; $k++) {
                    $ok = $true
                    for ($m = 0; $m -lt $old.Count; $m++) {
                        $x = $cur[$k + $m]; $y = $old[$m]
                        if ($trim) { $x = $x.Trim(); $y = $y.Trim() }
                        if ($x -ne $y) { $ok = $false; break }
                    }
                    if ($ok) { $at = $k; break }
                }
                if ($at -ge 0) { break }
            }
            if ($at -lt 0) { throw "apply_patch verification failed: Failed to find expected lines in ${full}:`n$($old -join "`n")" }
            $before = @(); if ($at -gt 0) { $before = @($cur[0..($at - 1)]) }
            $after = @(); if ($at + $old.Count -lt $cur.Count) { $after = @($cur[($at + $old.Count)..($cur.Count - 1)]) }
            $cur = @($before + $new + $after)
            $pos = $at + $new.Count
        }
        $txt = $cur -join "`n"
        if ($f.crlf) { $txt = $txt -replace "`n", "`r`n" }
        $dest = $full
        if ($op.move) { $dest = Resolve-ToolPath $op.move }
        $plan += @{ kind = 'M'; full = $full; dest = $dest; text = $txt; bom = $f.bom }
    }
    $done = @()
    foreach ($p in $plan) {
        if ($p.kind -eq 'D') { Push-Undo $p.full; Remove-Item -LiteralPath $p.full -Force; $done += "D $(Get-RelPath $p.full)"; continue }
        if ($p.kind -eq 'A') { Push-Undo $p.full; Write-TextFile $p.full $p.text $false; $done += "A $(Get-RelPath $p.full)"; continue }
        Push-Undo $p.full
        if ($p.dest -ne $p.full) { Push-Undo $p.dest; Write-TextFile $p.dest $p.text $p.bom; Remove-Item -LiteralPath $p.full -Force }
        else { Write-TextFile $p.full $p.text $p.bom }
        $done += "M $(Get-RelPath $p.dest)"
        $script:ReadFiles[(Get-FileKey $p.dest)] = 1
    }
    foreach ($d in $done) { Show-Sub $d 'Green' }
    return "Success. Updated the following files:`n" + ($done -join "`n")
}

# ---------------------------------------------------------------- todos
function Get-LatestTodos {
    # The list from the last todowrite call in the history; after older history
    # was dropped, the last list this session wrote.
    $latest = $null
    foreach ($m in $script:Messages) {
        if ($m.role -ne 'assistant') { continue }
        foreach ($tc in @(Get-Prop $m 'tool_calls')) {
            if ($null -eq $tc) { continue }
            if ($tc.function.name -ne 'todowrite') { continue }
            $args0 = $tc.function.arguments
            if ($args0 -is [string]) { try { $args0 = ConvertFrom-ArgsJson $args0 } catch { continue } }
            $td = Get-Prop $args0 'todos'
            if ($null -ne $td -and -not ($td -is [string])) { $latest = @($td) }
        }
    }
    if ($null -ne $latest) { $script:LastTodos = $latest; return $latest }
    return $script:LastTodos
}

function Format-TodosBlock($todos) {
    $items = @()
    foreach ($t in @($todos)) {
        if ($null -eq $t -or $t -is [string]) { continue }
        $content = ('' + (Get-Prop $t 'content')).Trim().Replace("`n", ' ')
        if (-not $content) { continue }
        $content = $content.Replace('<<<', '<<' + [string][char]0x200B + '<')
        if ($content.Length -gt 400) { $content = $content.Substring(0, 399).TrimEnd() + [string][char]0x2026 }
        $st = '' + (Get-Prop $t 'status')
        if (-not $st) { $st = 'pending' }
        $st = $st.Trim().ToLower()
        if (-not $script:TodoMarks.ContainsKey($st)) { $st = 'pending' }
        $items += , @($st, $content)
    }
    if ($items.Count -eq 0) { return $script:TX.NoTodos }
    $doneDrop = 0
    $laterDrop = 0
    while ($items.Count -gt 15) {
        $idx = -1
        for ($i = 0; $i -lt $items.Count; $i++) { if ($items[$i][0] -eq 'completed' -or $items[$i][0] -eq 'cancelled') { $idx = $i; break } }
        if ($idx -ge 0) { $doneDrop++ } else { $idx = $items.Count - 1; $laterDrop++ }
        $keep = @()
        for ($i = 0; $i -lt $items.Count; $i++) { if ($i -ne $idx) { $keep += , $items[$i] } }
        $items = $keep
    }
    $lines = @()
    foreach ($it in $items) { $lines += ('    [' + $script:TodoMarks[$it[0]] + '] ' + $it[1]) }
    $notes = @()
    if ($doneDrop) { $notes += "+$doneDrop finished item(s) not shown" }
    if ($laterDrop) { $notes += "+$laterDrop later item(s) not shown" }
    $elided = ''
    if ($notes.Count -gt 0) { $elided = "`n    (" + ($notes -join ', ') + ')' }
    return $script:TX.TodosHead + ($lines -join "`n") + $elided
}

function Get-OpenTodos {
    $open = @()
    foreach ($t in @(Get-LatestTodos)) {
        if ($null -eq $t -or $t -is [string]) { continue }
        if (('' + (Get-Prop $t 'status')).Trim().ToLower() -eq 'completed') { continue }
        $c = ('' + (Get-Prop $t 'content')).Trim()
        if (-not $c) { $c = '(unnamed item)' }
        $open += $c
    }
    return $open
}

function Get-TodoFocus($todos) {
    $items = @()
    foreach ($t in @($todos)) { if ($null -ne $t -and -not ($t -is [string]) -and ('' + (Get-Prop $t 'content')).Trim()) { $items += $t } }
    $pending = $false
    foreach ($t in $items) {
        $st = '' + (Get-Prop $t 'status')
        if (-not $st) { $st = 'pending' }
        $st = $st.Trim().ToLower()
        if ($st -eq 'in_progress') {
            $text = ('' + (Get-Prop $t 'content')).Trim().Replace("`n", ' ')
            if ($text.Length -gt 400) { $text = $text.Substring(0, 400) }
            return "Current item: $text. Stay on it until it is done and verified, then mark it completed with ``todowrite`` in the same message as the next item's first tool call. Do not drift into work that is not on the list; add it to the list first."
        }
        if ($st -eq 'pending') { $pending = $true }
    }
    if ($pending) { return 'No item is in progress: mark the next one in_progress with `todowrite` and work on it.' }
    return 'Every item is finished. If the request is fully done and verified, report the result; if anything is left, add it to the list first.'
}

# ---------------------------------------------------------------- history size
function Get-MsgSize($m) {
    $n = ('' + (Get-MessageText $m)).Length + 20
    foreach ($tc in @(Get-Prop $m 'tool_calls')) { if ($null -ne $tc) { $n += ('' + $tc.function.arguments).Length + 60 } }
    return $n
}

function Limit-History {
    # Drop the oldest messages (whole user / assistant+results units) once the
    # history passes the cap; the todo list survives in the reminder.
    $msgs = @($script:Messages)
    $sizes = @()
    $total = 0
    foreach ($m in $msgs) { $s = Get-MsgSize $m; $sizes += $s; $total += $s }
    if ($total -le $script:HistoryCap) { return }
    $cut = 0
    $acc = $total
    for ($i = 0; $i -lt $msgs.Count - 1; $i++) {
        $acc -= $sizes[$i]
        $r = $msgs[$i + 1].role
        if ($r -eq 'user' -or $r -eq 'assistant') { $cut = $i + 1; if ($acc -le $script:HistoryCap) { break } }
    }
    if ($cut -le 0) { return }
    $kept = @($msgs[$cut..($msgs.Count - 1)])
    $hasUser = $false
    foreach ($m in $kept) { if ($m.role -eq 'user') { $hasUser = $true; break } }
    if (-not $hasUser) {
        $lastUser = ''
        for ($i = $cut - 1; $i -ge 0; $i--) { if ($msgs[$i].role -eq 'user') { $lastUser = Get-MessageText $msgs[$i]; break } }
        if ($lastUser) {
            if ($lastUser.Length -gt 20000) { $lastUser = $lastUser.Substring(0, 20000) + "`n... (truncated)" }
            $kept = @(@{ role = 'user'; content = "[harness-lite: older messages were dropped to keep the request under the upstream's size limit. The user's request was:]`n`n$lastUser" }) + $kept
        }
    }
    $script:Messages = $kept
    Say "  (older history dropped: $cut messages, to stay under the request size limit)" 'DarkGray'
}

# ---------------------------------------------------------------- prompt mode (proxy port)
function Expand-Template([string]$tpl) {
    # Substitute {{TOKENS}} in one pass (expansions are not re-scanned).
    $out = ''
    $pos = 0
    while ($true) {
        $i = Find-Str $tpl '{{' $pos
        if ($i -lt 0) { break }
        $j = Find-Str $tpl '}}' ($i + 2)
        if ($j -lt 0) { break }
        $tok = $tpl.Substring($i, $j + 2 - $i)
        if ($script:Expand.ContainsKey($tok)) { $out += $tpl.Substring($pos, $i - $pos) + $script:Expand[$tok]; $pos = $j + 2 }
        else { $out += $tpl.Substring($pos, $i + 2 - $pos); $pos = $i + 2 }
    }
    return $out + $tpl.Substring($pos)
}

function Convert-History($convo) {
    # translate_history_and_apply_prompt, hybrid mode: OpenAI-shaped history ->
    # plain user/assistant text with the proxy's markers and reminder.
    $out = @()
    $idNames = @{}
    $sys = $script:A.System
    if ($sys) { $out += @{ role = 'system'; content = $sys } }
    foreach ($m in $convo) {
        $role = '' + $m.role
        if ($role -eq 'user') {
            $c = Get-MessageText $m
            if ($out.Count -gt 0 -and $out[$out.Count - 1].role -eq 'user') { $out[$out.Count - 1].content = $out[$out.Count - 1].content + "`n`n" + $c }
            else { $out += @{ role = 'user'; content = $c } }
        }
        elseif ($role -eq 'assistant') {
            $c = Get-MessageText $m
            foreach ($tc in @(Get-Prop $m 'tool_calls')) {
                if ($null -eq $tc) { continue }
                $nm = '' + $tc.function.name
                if ($tc.id) { $idNames['' + $tc.id] = $nm }
                $ar = $tc.function.arguments
                if ($ar -is [string]) { $at = $ar } else { $at = ConvertTo-PyJson $ar $true $false }
                $c += "`n``````json`n{`n  `"name`": `"$nm`",`n  `"arguments`": $at`n}`n```````n"
            }
            $out += @{ role = 'assistant'; content = $c.Trim() }
        }
        elseif ($role -eq 'tool') {
            $nm = '' + (Get-Prop $m 'name')
            if (-not $nm) { $nm = $idNames['' + (Get-Prop $m 'tool_call_id')] }
            if (-not $nm) { $nm = 'unknown_tool' }
            $block = "<<<BEGIN_TOOL_RESULT name=`"$nm`">>>`n$(Get-MessageText $m)`n<<<END_TOOL_RESULT>>>"
            if ($out.Count -gt 0 -and $out[$out.Count - 1].role -eq 'user') { $out[$out.Count - 1].content = $out[$out.Count - 1].content + "`n`n" + $block }
            else { $out += @{ role = 'user'; content = $block } }
        }
    }
    # Hybrid layout.
    if ($out.Count -gt 0 -and $out[0].role -eq 'system' -and $out[0].content.Trim()) {
        $out[0].content = "<<<BEGIN_AGENT_INSTRUCTIONS>>>`n" + $out[0].content + "`n<<<END_AGENT_INSTRUCTIONS>>>`n" + $script:A.SA
    }
    else { $out = @(@{ role = 'system'; content = $script:A.SA.Trim() }) + $out }
    $last = $out.Count - 1
    for ($i = 1; $i -lt $last; $i++) {
        if ($out[$i].role -eq 'user' -and -not $out[$i].content.Contains('<<<BEGIN_TOOL_RESULT')) {
            $out[$i].content = "<<<BEGIN_USER_MESSAGE>>>`n" + $out[$i].content + "`n<<<END_USER_MESSAGE>>>"
        }
    }
    if ($last -ge 1 -and $out[$last].role -eq 'user') {
        $c = $out[$last].content
        if ($c.Contains('<<<BEGIN_TOOL_RESULT')) { $wrapped = $c } else { $wrapped = "<<<BEGIN_USER_REQUEST>>>`n$c`n<<<END_USER_REQUEST>>>" }
        $script:Expand['{{TODOS}}'] = Format-TodosBlock (Get-LatestTodos)
        $script:Expand['{{TOOL_ENTRIES}}'] = $script:A.TE
        $out[$last].content = $wrapped + "`n`n" + (Expand-Template $script:TX.Reminder)
    }
    $out[0].content = $out[0].content + "`n`nSession: " + $script:SessionTag
    # System role -> user, with a stub assistant turn before the first real user message.
    $out[0].role = 'user'
    if ($out.Count -gt 1 -and $out[1].role -eq 'user') { $out = @($out[0], @{ role = 'assistant'; content = 'I understand the instructions above.' }) + @($out[1..($out.Count - 1)]) }
    return , $out
}

function Find-ToolCalls([string]$text) {
    # extract_tool_calls_and_text: every ```json {"name": ..., "arguments": ...} block.
    $payloads = @()
    $ranges = @()
    $pos = 0
    while ($true) {
        $fs = Find-Str $text '```json' $pos
        if ($fs -lt 0) { break }
        $bs = $fs + 7
        while ($bs -lt $text.Length -and " `t`n`r".IndexOf($text[$bs]) -ge 0) { $bs++ }
        $end = Find-JsonEnd $text $bs
        if ($end -lt 0) { $pos = $bs; continue }
        $after = $end + 1
        $rs = $after
        while ($rs -lt $text.Length -and " `t`n`r".IndexOf($text[$rs]) -ge 0) { $rs++ }
        $close = Find-Str $text '```' $rs
        if ($close -lt 0) { $blockEnd = $after }
        elseif ($text.Substring($close).StartsWith('```json')) { $blockEnd = $close }
        else { $blockEnd = $close + 3 }
        $obj = $null
        try { $obj = ConvertFrom-ArgsJson $text.Substring($bs, $end - $bs + 1) } catch { $obj = $null }
        if ($null -eq $obj -or $obj -is [string] -or $obj -is [array] -or -not (Test-Prop $obj 'name')) { $pos = $after; continue }
        $name = '' + (Get-Prop $obj 'name')
        if (Test-Prop $obj 'arguments') { if ($obj -is [hashtable]) { $ar = $obj['arguments'] } else { $ar = $obj.PSObject.Properties['arguments'].Value } }   # (Get-Prop would unroll a one-item array)
        else {
            if (-not $script:A.Names.ContainsKey($name)) { $pos = $after; continue }
            $ar = $obj | Select-Object -Property * -ExcludeProperty name
        }
        $payloads += @{ name = $name; arguments = $ar }
        $ranges += , @($fs, $blockEnd)
        $pos = $blockEnd
    }
    $clean = ''
    $last = 0
    foreach ($r in $ranges) { $clean += $text.Substring($last, $r[0] - $last); $last = $r[1] }
    $clean += $text.Substring($last)
    return @{ payloads = $payloads; clean = $clean.Trim() }
}

function Get-FailedCallKind([string]$text) {
    # _diagnose_failed_tool_call: '' | malformed_escape | malformed_fence
    if (-not $text) { return '' }
    $fp = Find-Str $text '```json' 0
    if ($fp -lt 0) { return '' }
    $bs = $fp + 7
    while ($bs -lt $text.Length -and " `t`n`r".IndexOf($text[$bs]) -ge 0) { $bs++ }
    if ($bs -lt $text.Length -and $text[$bs] -eq '{') {
        $end = Find-JsonEnd $text $bs
        if ($end -ge 0) {
            $js = $text.Substring($bs, $end - $bs + 1)
            if (Test-BadEscape $js) { return 'malformed_escape' }
        }
        return ''
    }
    if ($text.TrimStart().StartsWith('```json') -and (Find-Str $text '```json' ($fp + 1)) -lt 0) { return 'malformed_fence' }
    return ''
}

function Send-Prompt($convo, [string]$label) {
    $tr = Convert-History $convo
    $body = '{"model":' + (ConvertTo-JsonText $script:A.Model $false) + ',"messages":' + (ConvertTo-Json -InputObject @($tr) -Depth 5 -Compress) + '}'
    $chars = 0
    foreach ($m in $tr) { $chars += $m.content.Length }
    $script:LastCtx = [int]($chars / 4)
    $script:LastCtxEst = $true
    Show-Wait $label
    $resp = Invoke-Api 'POST' '/chat/completions' $body
    Clear-Wait
    $ch = @($resp.data.choices)[0]
    $script:LastFinishReason = '' + $ch.finish_reason
    return (Get-MessageText $ch.message)
}

function Invoke-RequireTool([string]$prevText, [string]$correction, [int]$budget, [string]$label) {
    # _serve_require_tool_correction: reject and ask again, up to $budget rounds.
    $convo = @($script:Messages)
    $t = $prevText
    for ($attempt = 1; $attempt -le $budget; $attempt++) {
        $msg = $correction
        if ($attempt -gt 1) { $msg += $script:TX.Escalation.Replace('{attempt}', "$attempt").Replace('{budget}', "$budget") }
        $convo = $convo + @(@{ role = 'assistant'; content = $t }, @{ role = 'user'; content = $msg })
        try { $t = Send-Prompt $convo "$label $($script:G.mid) asking again ($attempt/$budget)" }
        catch { Clear-Wait; Show-Sub "re-ask failed: $($_.Exception.Message)" 'DarkYellow'; return $null }
        $ex = Find-ToolCalls $t
        if ($ex.payloads.Count -gt 0 -and -not (Get-FailedCallKind $t)) { return @{ text = $t; ex = $ex } }
    }
    return $null
}

function Split-Finish($payloads) {
    $fin = $null
    $rest = @()
    foreach ($p in $payloads) { if ($null -eq $fin -and $p.name -eq 'finish') { $fin = $p; continue }; $rest += $p }
    if ($null -ne $fin -and $rest.Count -gt 0) { $fin = $null }   # real calls win; the run continues
    return @{ finish = $fin; rest = $rest }
}

function Get-FinishSummary($p) {
    $ar = $p.arguments
    if ($ar -is [string]) { return $ar.Trim() }
    foreach ($k in @('summary', 'message', 'text', 'content', 'result')) {
        $v = Get-Prop $ar $k
        if ($v -is [string] -and $v.Trim()) { return $v.Trim() }
    }
    return ''
}

function New-CallId { return 'call_' + ((New-Guid).ToString().Replace('-', '').Substring(0, 24)) }

function Invoke-PromptStep([int]$step) {
    $label = "thinking $($script:G.mid) step $step $($script:G.mid) $($script:ModelId)"
    $text = Send-Prompt $script:Messages $label
    $ex = Find-ToolCalls $text
    $retryUsed = 0
    if ($ex.payloads.Count -eq 0) {
        $kind = Get-FailedCallKind $text
        if ($kind) {
            $retryUsed = 1
            Show-Sub "the tool call was malformed ($kind); asking for it again" 'DarkGray'
            $fix = $script:TX.RetryFence
            if ($kind -eq 'malformed_escape') { $fix = $script:TX.RetryEscape }
            try {
                $t2 = Send-Prompt ($script:Messages + @(@{ role = 'assistant'; content = $text }, @{ role = 'user'; content = $fix })) "$label $($script:G.mid) retry"
                $ex2 = Find-ToolCalls $t2
                if ($ex2.payloads.Count -gt 0 -or -not (Get-FailedCallKind $t2)) { $text = $t2; $ex = $ex2 }
            }
            catch { Clear-Wait; Show-Sub "retry failed: $($_.Exception.Message)" 'DarkYellow' }
        }
    }
    $budget = 3 - $retryUsed
    if ($ex.payloads.Count -eq 0 -and $ex.clean.Trim() -and $budget -gt 0) {
        Show-Sub 'reply had no tool call; it was not shown and the model is asked again (require-tool)' 'DarkGray'
        $served = Invoke-RequireTool $text $script:TX.Correction $budget $label
        if ($served) { $text = $served.text; $ex = $served.ex }
    }
    $sp = Split-Finish $ex.payloads
    if ($null -ne $sp.finish) {
        $open = @(Get-OpenTodos)
        if ($open.Count -gt 0) {
            Show-Sub "finish held back: $(Get-Plural $open.Count 'todo item') still open" 'DarkGray'
            $corr = $script:TX.FinishTodos.Replace('{open_count}', "$($open.Count)").Replace('{next_item}', $open[0])
            $served = Invoke-RequireTool $text $corr 1 $label
            if ($served) { $text = $served.text; $ex = $served.ex; $sp = Split-Finish $ex.payloads }
        }
    }
    $clean = $ex.clean
    if ($null -ne $sp.finish) {
        $sum = Get-FinishSummary $sp.finish
        if ($sum) { $clean = $sum } elseif (-not $clean.Trim()) { $clean = $script:TX.EmptyFinish }
        $script:Messages += @{ role = 'assistant'; content = $clean }
        return @{ text = $clean; calls = @(); done = $true }
    }
    $rescue = $false
    $calls = $sp.rest
    if (-not $clean.Trim() -and $calls.Count -eq 0) {
        $rescue = $true
        $calls = @(@{ name = 'bash'; arguments = '{"command":"pwd","description":"Print working directory"}' })
        Show-Sub "empty reply (finish_reason '$($script:LastFinishReason)'); running pwd to move the turn on, as the proxy does" 'DarkGray'
    }
    if ($calls.Count -eq 0) {
        # require-tool failed open: the text goes to the user and the turn ends.
        $script:Messages += @{ role = 'assistant'; content = $clean }
        return @{ text = $clean; calls = @(); done = $true }
    }
    $tcs = @()
    $out = @()
    foreach ($p in $calls) {
        $id = New-CallId
        $ar = $p.arguments
        $nm = '' + $p.name
        $err = ''
        # opencode's repair: a wrong-case name is lowercased; an unknown tool
        # becomes a call to its `invalid` tool.
        $known = @($script:A.Req.Keys)
        if (-not ($known -ccontains $nm) -and ($known -ccontains $nm.ToLower())) { $nm = $nm.ToLower() }
        if (-not ($known -ccontains $nm)) {
            $msg = "Model tried to call unavailable tool '$nm'. Available tools: " + ((@($known + 'invalid') | Sort-Object) -join ', ') + '.'
            $at = '{"tool":' + (ConvertTo-JsonText $nm $false) + ',"error":' + (ConvertTo-JsonText $msg $false) + '}'
            $nm = 'invalid'
            $err = 'The arguments provided to the tool are invalid: ' + $msg
            $ar = $null
        }
        elseif ($ar -is [string]) { $at = $ar; try { $ar = ConvertFrom-ArgsJson $ar; $at = ConvertTo-PyJson $ar $false $false $true } catch { } }
        elseif ($null -eq $ar -or $ar -is [array]) {
            $gj = ConvertTo-PyJson $ar $false $false $true
            $at = '{"value":' + $gj + '}'
            $err = "The $nm tool was called with invalid arguments: SchemaError(Expected object, got $gj).`nPlease rewrite the input so it satisfies the expected schema."
        }
        else { $at = ConvertTo-PyJson $ar $false $false $true }
        $tcs += @{ id = $id; type = 'function'; function = @{ name = $nm; arguments = $at } }
        $out += @{ id = $id; name = $nm; a = $ar; err = $err; noAsk = $rescue }
    }
    $script:Messages += @{ role = 'assistant'; content = $clean; tool_calls = $tcs }
    return @{ text = $clean; calls = $out; done = $false; rescue = $rescue }
}

# ---------------------------------------------------------------- native mode (slim requests)
function Get-CallKey($tc) {
    $ar = $tc.function.arguments
    if ($ar -is [string]) { try { $ar = ConvertFrom-ArgsJson $ar } catch { } }
    return ('' + $tc.function.name) + ' ' + (ConvertTo-PyJson $ar $false $true)
}

function Get-NativeReminder {
    $lastUser = -1
    for ($i = 0; $i -lt $script:Messages.Count; $i++) { if ($script:Messages[$i].role -eq 'user') { $lastUser = $i } }
    $steps = 0
    $sinceEdit = 0
    $sinceTodo = -1
    $calls = @{}
    for ($j = $lastUser + 1; $j -lt $script:Messages.Count; $j++) {
        $m = $script:Messages[$j]
        if ($m.role -ne 'assistant') { continue }
        $steps++
        $names = @{}
        foreach ($tc in @(Get-Prop $m 'tool_calls')) {
            if ($null -eq $tc) { continue }
            $names['' + $tc.function.name] = 1
            $k = Get-CallKey $tc
            if ($calls.ContainsKey($k)) { $calls[$k]++ } else { $calls[$k] = 1 }
        }
        if ($names.ContainsKey('edit') -or $names.ContainsKey('write') -or $names.ContainsKey('apply_patch')) { $sinceEdit = 0 } else { $sinceEdit++ }
        if ($names.ContainsKey('todowrite')) { $sinceTodo = 0 } elseif ($sinceTodo -ge 0) { $sinceTodo++ }
    }
    $lines = @("harness: step $steps of this task.")
    $todos = @(Get-LatestTodos | Where-Object { $null -ne $_ })
    if ($todos.Count -gt 0) {
        $lines += (Format-TodosBlock $todos).Trim()
        if ($sinceTodo -ge 12) { $lines += "$sinceTodo steps since you last updated the list. Call ``todowrite`` now: mark what is done, keep one item in progress, and add anything new you found. If you are stuck on the current item, say what blocks you." }
        else { $lines += (Get-TodoFocus $todos) }
    }
    elseif ($script:A.HasTodo -and $steps -ge 3) { $lines += 'You have no todo list. Call `todowrite` now with the concrete steps of the user''s request, including the change itself and how you will verify it, then work through it.' }
    elseif ($script:A.HasTodo) { $lines += 'Unless this is a single trivial step, call `todowrite` with every step of the request, including how you will verify the change, in the same message as your first other tool calls.' }
    $rep = @()
    foreach ($k in $calls.Keys) { if ($calls[$k] -ge 3) { $rep += @{ n = $calls[$k]; k = $k } } }
    if ($rep.Count -gt 0) {
        $rep = @($rep | Sort-Object -Property @{ Expression = { $_.n }; Descending = $true }, @{ Expression = { $_.k }; Descending = $true } | Select-Object -First 3)
        $shown = @()
        foreach ($r in $rep) { $kk = $r.k; if ($kk.Length -gt 160) { $kk = $kk.Substring(0, 160) }; $shown += "$kk (x$($r.n))" }
        $lines += "You have made these exact tool calls more than once in this task: $($shown -join '; '). Their results are already in this conversation unless marked as dropped. Do not repeat a call to get the same answer; use what you have or try something different."
    }
    if ($sinceEdit -ge 30) { $lines += "$sinceEdit steps without changing a file. That is fine while the current todo item needs research; if it needs a change, make it now." }
    return "<system-reminder>`n" + ($lines -join "`n") + "`n</system-reminder>"
}

function Get-NativeMessages {
    # The history as sent: old tool output trimmed (cache-friendly steps), the
    # reminder appended to the last message, the system prompt first.
    $msgs = @($script:Messages)
    $lastAsst = -1
    for ($i = 0; $i -lt $msgs.Count; $i++) { if ($msgs[$i].role -eq 'assistant') { $lastAsst = $i } }
    $total = 0
    foreach ($m in $msgs) { if ($m.role -eq 'tool') { $total += (Get-MessageText $m).Length } }
    $picked = @{}
    $budget = 120000
    if ($total -gt $budget) {
        $over = $total - $budget
        $boundary = $over - ($over % 60000)
        $seen = 0
        for ($i = 0; $i -lt $msgs.Count; $i++) {
            if ($msgs[$i].role -ne 'tool') { continue }
            $t = Get-MessageText $msgs[$i]
            $seen += $t.Length
            if ($seen -gt $boundary) { break }
            $nm = '' + (Get-Prop $msgs[$i] 'name')
            if ($i -lt $lastAsst -and $nm -ne 'todowrite' -and $nm -ne 'task' -and $t.Length -gt 1500) { $picked[$i] = 1 }
        }
    }
    $out = @(@{ role = 'system'; content = $script:A.NSystem })
    for ($i = 0; $i -lt $msgs.Count; $i++) {
        $m = $msgs[$i]
        if ($m.role -eq 'tool') {
            $c = Get-MessageText $m
            if ($picked.ContainsKey($i)) {
                $cut = $c.Length - 500
                $c = $c.Substring(0, 500) + "`n[harness: $(Format-Thousands $cut) more chars of this older tool output were dropped to save tokens. Run the tool again if you need them.]"
            }
            $out += @{ role = 'tool'; tool_call_id = '' + (Get-Prop $m 'tool_call_id'); content = $c }
        }
        elseif ($m.role -eq 'assistant') {
            $a2 = @{ role = 'assistant'; content = (Get-Prop $m 'content') }
            $tcs = Get-Prop $m 'tool_calls'
            if ($null -ne $tcs -and @($tcs).Count -gt 0) { $a2['tool_calls'] = @($tcs) }
            $out += $a2
        }
        else { $out += @{ role = '' + $m.role; content = Get-MessageText $m } }
    }
    $li = $out.Count - 1
    $out[$li] = @{} + $out[$li]
    # The proxy's slim rules: general is capped at 15 steps (tools off, a note
    # instead of the reminder); every other step gets the reminder.
    $steps = 0
    for ($i = $msgs.Count - 1; $i -ge 0; $i--) { if ($msgs[$i].role -eq 'user') { break }; if ($msgs[$i].role -eq 'assistant') { $steps++ } }
    $script:A.Capped = ($script:A.Kind -eq 'general' -and $steps -ge 15)
    if ($script:A.Capped) { $out[$li].content = ('' + $out[$li].content) + "`n`n" + $script:TX.SlimCapNote.Replace('{n}', '15') }
    else { $out[$li].content = ('' + $out[$li].content) + "`n`n" + (Get-NativeReminder) }
    return $out
}

function Invoke-NativeStep([int]$step) {
    $label = "thinking $($script:G.mid) step $step $($script:G.mid) $($script:ModelId)"
    $msgs = Get-NativeMessages
    $choice = 'auto'
    if ($script:A.Capped) { $choice = 'none' }
    $body = '{"model":' + (ConvertTo-JsonText $script:A.Model $false) + ',"max_tokens":32000,"messages":' + (ConvertTo-Json -InputObject @($msgs) -Depth 30 -Compress) +
    ',"tools":' + $script:A.NT + ',"tool_choice":"' + $choice + '"}'
    Show-Wait $label
    $resp = Invoke-Api 'POST' '/chat/completions' $body
    Clear-Wait
    $ch = @($resp.data.choices)[0]
    $script:LastFinishReason = '' + $ch.finish_reason
    $msg = $ch.message
    $text = Get-MessageText $msg
    $raw = @()
    if ($msg.tool_calls) { $raw = @($msg.tool_calls) }
    $calls = @()
    foreach ($c in $raw) {
        $a = $null
        $err = ''
        $fa = $c.function.arguments
        if ($null -eq $fa -or $fa -is [string]) {
            try { $a = ConvertFrom-ArgsJson ('' + $fa) } catch { $err = "The $($c.function.name) tool was called with invalid arguments: they are not valid JSON ($($_.Exception.Message)). Please rewrite the input so it satisfies the expected schema." }
        }
        else { $a = $fa; $c.function.arguments = ConvertTo-PyJson $fa $false $false $true }
        $calls += @{ id = '' + $c.id; name = '' + $c.function.name; a = $a; err = $err; noAsk = $false }
    }
    if ($text -or $raw.Count -gt 0) {
        $content = $null
        if ($text) { $content = $text }
        $assistant = @{ role = 'assistant'; content = $content }
        if ($raw.Count -gt 0) { $assistant['tool_calls'] = $raw }
        $script:Messages += $assistant
    }
    $script:LastCtx = 0
    $script:LastCtxEst = $false
    if ($resp.data.usage -and $resp.data.usage.prompt_tokens) { $script:LastCtx = [int]$resp.data.usage.prompt_tokens }
    return @{ text = $text; calls = $calls; done = ($calls.Count -eq 0) }
}

# ---------------------------------------------------------------- models
$script:Catalog = @{}
$script:CatalogIds = @()

function Get-Catalog {
    $resp = Invoke-Api 'GET' '/models' $null
    $ids = @()
    foreach ($m in @($resp.data.data)) {
        if (-not $m.id) { continue }
        if ($m.capabilities -and $m.capabilities.chat -eq $false) { continue }
        $w = ''
        if ($m.credit_weights) { $w = "$($m.credit_weights.input)/$($m.credit_weights.output)" }
        $script:Catalog[$m.id] = @{ lane = '' + $m.lane; w = $w }
        $ids += $m.id
    }
    $script:CatalogIds = $ids
}

function Get-ModeFor([string]$id) {
    # The harness proxy's rule: only the -withtools family takes native tools.
    if ($id -match '-withtools$') { return 'native' }
    return 'prompt'
}

function Get-ModelNote([string]$id) {
    $info = $script:Catalog[$id]
    $free = Test-FreeModel $id
    $s = 'native tools'
    if ((Get-ModeFor $id) -eq 'prompt') { $s = 'prompt mode (require-tool)' }
    if ($free) { $s += ', free' } elseif ($info -and $info.w) { $s += ", $($info.w) credits per token in/out" }
    return $s
}

function Get-LastModel {
    if ($script:Cfg.model) { return $script:Cfg.model }
    try { if (Test-Path -LiteralPath $script:LastModelFile) { return ('' + (Get-Content -LiteralPath $script:LastModelFile -Raw)).Trim() } } catch { }
    return ''
}

function Select-Model {
    if ($script:CatalogIds.Count -eq 0) { Get-Catalog }
    $ids = $script:CatalogIds
    if ($ids.Count -eq 0) { throw 'the API listed no chat models' }
    $last = Get-LastModel
    $def = ''
    if ($last -and $ids -contains $last) { $def = $last }
    if (-not $def) { foreach ($id in $ids) { if ($id -match '-withtools$') { $def = $id; break } } }
    if (-not $def) { $def = $ids[0] }
    $padTo = 0
    foreach ($id in $ids) { if ($id.Length -gt $padTo) { $padTo = $id.Length } }
    WL ''
    for ($i = 0; $i -lt $ids.Count; $i++) {
        $mark = '  '
        if ($ids[$i] -eq $def) { $mark = $script:G.prompt + ' ' }
        W ('  ' + $mark + ('{0,3}' -f ($i + 1)) + '  ') 'DarkGray'
        W ($ids[$i].PadRight($padTo + 2))
        WL (Get-ModelNote $ids[$i]) 'DarkGray'
    }
    W "  model number or id, Enter for $def " 'DarkGray'
    W ($script:G.prompt + ' ') 'Cyan'
    $pick = Read-Host
    if ($null -eq $pick) { $pick = '' }
    $pick = $pick.Trim()
    if (-not $pick) { return $def }
    if ($pick -match '^\d+$') {
        $n = [int]$pick
        if ($n -ge 1 -and $n -le $ids.Count) { return $ids[$n - 1] }
    }
    return $pick
}

function Set-Model([string]$id) {
    # Both modes keep the same OpenAI-shaped history, so switching keeps the conversation.
    $script:ModelId = $id
    $script:Mode = Get-ModeFor $id
    try { Set-Content -LiteralPath $script:LastModelFile -Value $id -Encoding ASCII } catch { }
    if ($script:TX) { Initialize-Environment }
}

# ---------------------------------------------------------------- conversation
function Reset-Conversation {
    $script:Messages = @()
    $script:Undo = @()
    $script:PendingNote = ''
    $script:LastTodos = $null
    $script:ReadFiles = @{}
    $script:SessionTag = (New-Guid).ToString().Replace('-', '').Substring(0, 12)
}

function Save-Session {
    if ($script:Messages.Count -eq 0) { return }
    try {
        $o = @{ version = 3; model = $script:ModelId; root = $script:RootFull; saved = (Get-Date -Format 's'); tag = $script:SessionTag
            todos = $script:LastTodos; messages = $script:Messages }
        Set-Content -LiteralPath ($script:SessionFile + '.tmp') -Value (ConvertTo-AsciiJson ($o | ConvertTo-Json -Depth 40 -Compress)) -Encoding ASCII
        Move-Item -LiteralPath ($script:SessionFile + '.tmp') -Destination $script:SessionFile -Force
    }
    catch { }
}

function Restore-Session {
    if (-not (Test-Path -LiteralPath $script:SessionFile)) { Say '  no saved conversation for this folder' 'DarkGray'; return }
    try { $o = Get-Content -LiteralPath $script:SessionFile -Raw | ConvertFrom-Json }
    catch { Say "  cannot read the saved conversation: $($_.Exception.Message)" 'Red'; return }
    if ($o.version -ne 3) { Say '  the saved conversation is from an older harness-lite and cannot be resumed' 'DarkYellow'; return }
    $saved = @($o.messages)
    $script:Messages = $saved
    $script:Undo = @()
    if ($o.tag) { $script:SessionTag = '' + $o.tag }
    if ($null -ne $o.todos) { $script:LastTodos = @($o.todos) }
    $lastUser = ''
    foreach ($m in $saved) { if ($m.role -eq 'user') { $lastUser = Get-MessageText $m } }
    Say "  resumed $($saved.Count) messages from $($o.saved)" 'DarkGray'
    if ($lastUser) { Say ('  last request: ' + (Limit-Line ($lastUser -replace '\s+', ' ') ((Get-Width) - 20))) 'DarkGray' }
}

function Show-TurnStatus([int]$steps, $t0, $creditsBefore) {
    $parts = @()
    $parts += (Get-Plural $steps 'step')
    $parts += ('{0:N1}s' -f ((Get-Date) - $t0).TotalSeconds)
    if ($script:LastCtx) {
        $k = [int]($script:LastCtx / 1000)
        $est = ''
        if ($script:LastCtxEst) { $est = ' (est.)' }
        $parts += "context ~${k}k tokens$est"
    }
    if ($null -ne $script:CreditsLeft) {
        $c = '{0:N0} credits left' -f $script:CreditsLeft
        if ($null -ne $creditsBefore -and $creditsBefore -gt $script:CreditsLeft) { $c += (' (-{0:N0})' -f ($creditsBefore - $script:CreditsLeft)) }
        $parts += $c
    }
    WL ('  ' + ($parts -join " $($script:G.mid) ")) 'DarkGray'
}

function Invoke-AgentLoop([int]$limit) {
    # Steps until the agent answers without a tool call; used by the main
    # agent and by task subagents (with their own context in $script:A).
    $empties = 0
    $rescues = 0
    $step = 0
    $final = ''
    while ($true) {
        $step++
        if ($step -gt $limit) { Say "  stopped after $limit steps; say 'continue' to keep going" 'DarkYellow'; break }
        Limit-History
        WL ''
        if ($script:A.Mode -eq 'native') { $r = Invoke-NativeStep $step } else { $r = Invoke-PromptStep $step }
        if ($r.text -and $r.text.Trim()) { Write-Markdown $r.text; $final = $r.text }
        if ($r.rescue) { $rescues++ } else { $rescues = 0 }
        if ($r.calls.Count -eq 0) {
            if (($r.text -and $r.text.Trim()) -or $script:A.Mode -ne 'native') { break }
            $empties++
            if ($empties -le 1) { Say "  (empty reply, finish_reason '$($script:LastFinishReason)'; asking again)" 'DarkYellow'; continue }
            Say "  The model sent back an empty reply twice (finish_reason '$($script:LastFinishReason)'). Rephrase, /clear, or pick another model with /model." 'DarkYellow'
            break
        }
        foreach ($c in $r.calls) {
            $result = ''
            if ($c.err) {
                WL ($script:G.dot + ' tool call problem') 'Red'
                Show-Sub $c.err 'Red'
                $result = $c.err
            }
            else {
                $script:CurCall = '' + $c.id
                Show-ToolCall $c.name $c.a
                try { $result = '' + (Invoke-Tool $c.name $c.a ([bool]$c.noAsk)) }
                catch { $result = '' + $_.Exception.Message; if ($c.name -eq 'task' -and $result -match '^HTTP (401|403) ') { throw $result }; Show-Sub ('Error: ' + @(Get-Lines $result)[0]) 'Red'; $toolErr = $true }
            }
            $script:Messages += @{ role = 'tool'; tool_call_id = $c.id; name = $c.name; content = $result }
            if ($script:GuiOn) { Send-Ui @{ t = 'tool_end'; id = ('' + $c.id); name = ('' + $c.name); error = [bool]($c.err -or $toolErr) }; $toolErr = $false; if ($c.name -eq 'todowrite') { Show-Todos } }
        }
        if (-not $script:A.Sub) { Save-Session }
    }
    return @{ text = $final; steps = $step }
}

function Invoke-Turn([string]$userText, [bool]$retry) {
    # $retry: the user message is already in the history (auth failure, config re-entered).
    if (-not $retry) {
        if ($script:PendingNote) { $userText = $script:PendingNote + "`n" + $userText; $script:PendingNote = '' }
        $script:Messages += @{ role = 'user'; content = $userText }
    }
    $t0 = Get-Date
    $creditsBefore = $script:CreditsLeft
    $r = @{ steps = 0 }
    try { $r = Invoke-AgentLoop $MaxSteps }
    finally {
        Clear-Wait
        Save-Session
    }
    Show-TurnStatus $r.steps $t0 $creditsBefore
}

function Undo-Last {
    if ($script:Undo.Count -eq 0) { Say '  nothing to undo' 'DarkGray'; return }
    $u = $script:Undo[$script:Undo.Count - 1]
    if ($script:Undo.Count -gt 1) { $script:Undo = @($script:Undo[0..($script:Undo.Count - 2)]) } else { $script:Undo = @() }
    $rel = Get-RelPath $u.path
    if ($u.existed) { Write-TextFile $u.path $u.text $u.bom; Say "  restored $rel" 'Green' }
    else { if (Test-Path -LiteralPath $u.path) { Remove-Item -LiteralPath $u.path -Force }; Say "  removed $rel (the agent had created it)" 'Green' }
    $script:PendingNote += "[harness-lite note: the user undid your last change to $($u.path); it is back to how it was before that change.]`n"
}

function Read-UserInput {
    WL ''
    W ($script:G.prompt + ' ') 'Cyan'
    $line = Read-Host
    if ($null -eq $line) { return $null }
    if ($line.Trim() -eq '<<<') {
        Say '  multi-line: end with >>> on its own line' 'DarkGray'
        $buf = @()
        while ($true) { $l = Read-Host; if ($null -eq $l -or $l.Trim() -eq '>>>') { break }; $buf += $l }
        return ($buf -join "`n")
    }
    # A paste of several lines arrives as several Enter presses: keep reading
    # while more input is already waiting.
    $buf = @($line)
    while ($true) {
        $more = $false
        try { Start-Sleep -Milliseconds 40; $more = [Console]::KeyAvailable } catch { }
        if (-not $more) { break }
        $l = Read-Host
        if ($null -eq $l) { break }
        $buf += $l
    }
    if ($buf.Count -gt 1) { Say "  ($($buf.Count) lines)" 'DarkGray' }
    return ($buf -join "`n")
}

$script:Commands = @(
    @('/help', 'list the commands'),
    @('/new', 'start a new conversation (also /clear)'),
    @('/models [n|id]', 'switch model (lists them when no id; also /model); the conversation is kept'),
    @('/undo', 'revert the agent''s last file change (repeat for more)'),
    @('/sessions', 'list the saved conversations'),
    @('/resume', 'reload the last saved conversation for this folder'),
    @('/export [file]', 'write the conversation to a markdown file in this folder'),
    @('/todos', 'show the agent''s todo list'),
    @('/auto', 'toggle running commands without asking'),
    @('/exit', 'quit (the conversation is saved)')
)

function Show-Help {
    foreach ($r in $script:Commands) { W ('  ' + $r[0].PadRight(16)) 'Cyan'; WL $r[1] 'DarkGray' }
    Say "  run commands without asking is $(if ($script:AutoApproveOn) { 'on' } else { 'off' }). Paste multi-line text directly, or type <<< , the lines, then >>>." 'DarkGray'
}

function Show-Todos {
    $t = @(Get-LatestTodos | Where-Object { $null -ne $_ })
    if ($t.Count -eq 0) { Say '  no todo list yet' 'DarkGray'; return }
    foreach ($x in $t) {
        $st = ('' + (Get-Prop $x 'status')).Trim().ToLower()
        if (-not $script:TodoMarks.ContainsKey($st)) { $st = 'pending' }
        $c = 'Gray'
        if ($st -eq 'completed' -or $st -eq 'cancelled') { $c = 'DarkGray' } elseif ($st -eq 'in_progress') { $c = 'Yellow' }
        WL ("  [" + $script:TodoMarks[$st] + "] " + (('' + (Get-Prop $x 'content')) -replace '\s+', ' ').Trim()) $c
    }
}

function Show-Banner {
    Show-Rule
    W ' harness-lite' 'Cyan'
    WL "  $($script:G.mid) v$($script:Version) $($script:G.mid) harness in one PowerShell file" 'DarkGray'
    W ' model   ' 'DarkGray'; W $script:ModelId; WL "  $($script:G.mid) $(Get-ModelNote $script:ModelId)" 'DarkGray'
    W ' folder  ' 'DarkGray'; W $script:RootFull
    if ($script:InstrFiles.Count -gt 0) { WL "  $($script:G.mid) $(Get-Plural $script:InstrFiles.Count 'instruction file') $($script:G.mid) $(Get-Plural $script:Skills.Count 'skill')" 'DarkGray' } else { WL "  $($script:G.mid) $(Get-Plural $script:Skills.Count 'skill')" 'DarkGray' }
    $ask = 'commands ask first'
    if ($script:AutoApproveOn) { $ask = 'commands auto-approved' }
    $sh = "Git Bash ($($script:Shell.exe))"
    if ($script:Shell.kind -ne 'bash') { $sh = "PowerShell $($PSVersionTable.PSVersion) ($($script:Shell.why))" }
    if (-not $script:OnWindows -and $script:Shell.kind -eq 'bash') { $sh = "bash ($($script:Shell.exe))" }
    W ' shell   ' 'DarkGray'; WL "$sh $($script:G.mid) $ask"
    Show-Rule
}

function Invoke-UserTurn([string]$line) {
    # One user turn; an auth failure re-asks for the config and retries without re-adding the message.
    $retry = $false
    while ($true) {
        $err = ''
        try { if ($script:GuiOn) { Invoke-GuiTurn $line $retry } else { Invoke-Turn $line $retry } }
        catch { Clear-Wait; $err = '' + $_.Exception.Message; Say "  error: $err" 'Red' }
        if ($Prompt -or $err -notmatch '^HTTP (401|403) ') { return (-not $err) }
        Confirm-Config $err "POST $($script:ApiRoot)/chat/completions"
        $retry = $true
    }
}

function Show-Sessions {
    $n = 0
    foreach ($f in @(Get-ChildItem -LiteralPath $script:StateDir -Filter 'session-*.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
        try { $o = Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json } catch { continue }
        $n++
        $here = ''
        if ($f.FullName -eq $script:SessionFile) { $here = '  (this folder: /resume)' }
        W ('  ' + $o.saved + '  ') 'DarkGray'; W ('' + $o.root); WL ("  $($script:G.mid) $(@($o.messages).Count) messages $($script:G.mid) $($o.model)$here") 'DarkGray'
    }
    if ($n -eq 0) { Say '  no saved conversations' 'DarkGray' }
}

function Export-Conversation([string]$name) {
    if (-not $name) { $name = 'harness-lite-export-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.md' }
    $out = @()
    foreach ($m in $script:Messages) {
        $role = '' + (Get-Prop $m 'role')
        $text = '' + (Get-MessageText $m)
        foreach ($c in @(Get-Prop $m 'tool_calls' | Where-Object { $_ })) { $f = Get-Prop $c 'function'; $text += "`n`n[tool call] $(Get-Prop $f 'name') $(Get-Prop $f 'arguments')" }
        $out += "## $role"; $out += ''; $out += $text; $out += ''
    }
    $path = Join-Path $script:RootFull $name
    Set-Content -LiteralPath $path -Value ($out -join "`n") -Encoding UTF8
    Say "  exported $($script:Messages.Count) messages to $path" 'Green'
}

function Invoke-Line([string]$line) {
    # One line of user input: a slash command or a turn. Returns $false to quit.
    $t = $line.Trim()
    if (-not $t) { return $true }
    if ($t -eq '/exit' -or $t -eq '/quit' -or $t -eq '/q') { return $false }
    if ($t -eq '/help' -or $t -eq '/?') { Show-Help; return $true }
    if ($t -eq '/todos') { Show-Todos; return $true }
    if ($t -eq '/clear' -or $t -eq '/new') { Reset-Conversation; if ($script:GuiOn) { Send-Ui @{ t = 'reset' } }; Say '  new conversation' 'DarkGray'; return $true }
    if ($t -eq '/undo') { try { Undo-Last } catch { Say "  undo failed: $($_.Exception.Message)" 'Red' }; return $true }
    if ($t -eq '/resume') { Restore-Session; if ($script:GuiOn) { Send-UiHistory }; return $true }
    if ($t -eq '/sessions') { Show-Sessions; return $true }
    if ($t -match '^/export\s*(.*)$') { try { Export-Conversation $Matches[1].Trim() } catch { Say "  export failed: $($_.Exception.Message)" 'Red' }; return $true }
    if ($t -eq '/auto') { $script:AutoApproveOn = -not $script:AutoApproveOn; Say "  run commands without asking: $(if ($script:AutoApproveOn) { 'on' } else { 'off' })" 'DarkGray'; return $true }
    if ($t -match '^/models?\s*(.*)$') {
        $m = $Matches[1].Trim()
        try {
            if (-not $m) { $m = Select-Model }
            elseif ($m -match '^\d+$' -and [int]$m -ge 1 -and [int]$m -le $script:CatalogIds.Count) { $m = $script:CatalogIds[[int]$m - 1] }
            if (-not $m) { return $true }
            Set-Model $m
            $script:Cfg.model = $m; Save-Config
            Say "  model $m $($script:G.mid) $(Get-ModelNote $m)" 'DarkGray'
            if ($script:GuiOn) { Send-Ui @{ t = 'model'; id = $m; note = (Get-ModelNote $m); mode = 'build' } }
        }
        catch { Say "  $($_.Exception.Message)" 'Red' }
        return $true
    }
    if ($t.StartsWith('/') -and $t -match '^/\w+$') { Say "  unknown command $t; /help lists them" 'DarkYellow'; return $true }
    $null = Invoke-UserTurn $line
    return $true
}

# ---------------------------------------------------------------- GUI bridge
# Local page served on 127.0.0.1 by a background runspace; the engine stays on
# this thread and talks to it through two queues. Needs FullLanguage: in
# ConstrainedLanguage none of this runs and the console UI is used.
$script:UiPage = @'
#@@UI@@
'@

function Start-UiServer {
    $B = [hashtable]::Synchronized(@{ Stop = $false; Cur = $null; Connected = $false; Sse = $false; Bye = $false; Err = ''; Page = $script:UiPage; LastSeen = [datetime]::UtcNow })
    $B.Token = [guid]::NewGuid().ToString('N')
    $B.Nonce = [guid]::NewGuid().ToString('N')
    $B.Out = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
    $B.In = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
    $port = 0
    if ($env:HARNESS_LITE_GUI_PORT) { $port = [int]$env:HARNESS_LITE_GUI_PORT }
    $B.L = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $port)
    $B.L.Start()
    $B.Port = ([System.Net.IPEndPoint]$B.L.LocalEndpoint).Port
    $srv = {
        param($B)
        try {
            $l = $B.L; $sse = $null; $sseC = $null; $enc = [System.Text.Encoding]::UTF8; $log = New-Object System.Collections.ArrayList; $ping = [datetime]::UtcNow
            $pend = New-Object System.Collections.ArrayList; $buf = New-Object byte[] 65536
            function Send($s, [string]$status, [string]$type, [string]$body, [string]$extra) {
                $bb = $enc.GetBytes($body)
                $hb = $enc.GetBytes("HTTP/1.1 $status`r`nContent-Type: $type`r`nContent-Length: $($bb.Length)`r`nCache-Control: no-store`r`n${extra}Connection: close`r`n`r`n")
                $s.Write($hb, 0, $hb.Length); $s.Write($bb, 0, $bb.Length); $s.Flush()
            }
            function Halt { $B.Bye = $true; $p = $B.Cur; if ($p) { try { $null = $p.BeginStop($null, $null) } catch { } } }
            while (-not $B.Stop) {
                while ($l.Pending()) {
                    $c = $l.AcceptTcpClient(); $c.NoDelay = $true; $c.SendTimeout = 2000
                    $null = $pend.Add(@{ c = $c; t = [datetime]::UtcNow; ms = (New-Object System.IO.MemoryStream) })
                }
                # Browsers open idle preconnect sockets: only read a socket that has bytes waiting.
                foreach ($q in @($pend)) {
                    $c = $q.c
                    try {
                        if ($c.Available -le 0) { if (([datetime]::UtcNow - $q.t).TotalSeconds -gt 10) { $pend.Remove($q); $c.Close() }; continue }
                        $s = $c.GetStream(); $n = $s.Read($buf, 0, $buf.Length); $q.ms.Write($buf, 0, $n)
                        $all = $q.ms.ToArray(); $txt = [System.Text.Encoding]::GetEncoding(28591).GetString($all); $i = $txt.IndexOf("`r`n`r`n")
                        if ($i -lt 0) { continue }
                        $head = $txt.Substring(0, $i); $cl = 0
                        if ($head -match '(?im)^Content-Length:\s*(\d+)') { $cl = [int]$Matches[1] }
                        if ($all.Length -lt $i + 4 + $cl) { continue }
                        $body = $enc.GetString($all, $i + 4, $cl)
                        $pend.Remove($q)
                        $line = ($head -split "`r`n")[0]
                        $tok = ''
                        if ($line -match '[?&]t=([0-9A-Za-z]+)') { $tok = $Matches[1] }
                        if ($head -match '(?im)^X-Token:\s*(\S+)') { $tok = $Matches[1] }
                        if ($tok -cne $B.Token -or $head -notmatch ('(?im)^Host:\s*127\.0\.0\.1:' + $B.Port + '\s*$')) { Send $s '403 Forbidden' 'text/plain' 'forbidden' ''; $c.Close() }
                        elseif ($line -match '^GET /events') {
                            $from = 0
                            if ($line -match '[?&]since=(\d+)') { $from = [int]$Matches[1] }
                            $hb = $enc.GetBytes("HTTP/1.1 200 OK`r`nContent-Type: text/event-stream`r`nCache-Control: no-store`r`n`r`n"); $s.Write($hb, 0, $hb.Length)
                            for ($k = $from; $k -lt $log.Count; $k++) { $eb = $enc.GetBytes("id: $($k + 1)`ndata: $($log[$k])`n`n"); $s.Write($eb, 0, $eb.Length) }
                            $s.Flush()
                            if ($sseC) { try { $sseC.Close() } catch { } }
                            $sse = $s; $sseC = $c; $B.Connected = $true
                        }
                        elseif ($line -match '^POST /send') {
                            if ($body -match '^\s*\{\s*"type"\s*:\s*"interrupt"') { $p = $B.Cur; if ($p) { try { $null = $p.BeginStop($null, $null) } catch { } } }
                            elseif ($body -match '^\s*\{\s*"type"\s*:\s*"bye"') { Halt }
                            else { $B.In.Enqueue($body) }
                            Send $s '204 No Content' 'text/plain' '' ''; $c.Close()
                        }
                        elseif ($line -match '^GET /(\?| )') {
                            $csp = "Content-Security-Policy: default-src 'none'; script-src 'nonce-$($B.Nonce)'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:`r`n"
                            Send $s '200 OK' 'text/html; charset=utf-8' $B.Page.Replace('__NONCE__', $B.Nonce) $csp; $c.Close()
                        }
                        else { Send $s '404 Not Found' 'text/plain' 'not found' ''; $c.Close() }
                    }
                    catch { $B.Err = '' + $_.Exception.Message; $pend.Remove($q); try { $c.Close() } catch { } }
                }
                # A closed window shows as a readable socket with nothing to read.
                if ($sseC) { try { if ($sseC.Client.Poll(0, [System.Net.Sockets.SelectMode]::SelectRead) -and $sseC.Available -eq 0) { $sseC.Close(); $sse = $null; $sseC = $null } } catch { $sse = $null; $sseC = $null } }
                $e = $null
                while ($B.Out.TryDequeue([ref]$e)) {
                    $null = $log.Add($e)
                    if ($sse) { $eb = $enc.GetBytes("id: $($log.Count)`ndata: $e`n`n"); try { $sse.Write($eb, 0, $eb.Length); $sse.Flush() } catch { $sse = $null; $sseC = $null } }
                }
                $now = [datetime]::UtcNow
                if ($sse -and ($now - $ping).TotalSeconds -ge 5) { $ping = $now; $eb = $enc.GetBytes(": ping`n`n"); try { $sse.Write($eb, 0, $eb.Length); $sse.Flush() } catch { $sse = $null; $sseC = $null } }
                $B.Sse = [bool]$sse
                if ($sse) { $B.LastSeen = $now } elseif ($B.Connected -and -not $B.Bye -and ($now - $B.LastSeen).TotalSeconds -gt 10) { Halt }
                Start-Sleep -Milliseconds 15
            }
            $l.Stop()
        }
        catch { $B.Err = 'server stopped: ' + $_.Exception.Message; $B.Bye = $true }
    }
    $rs = [runspacefactory]::CreateRunspace(); $rs.Open()
    $ps = [powershell]::Create(); $ps.Runspace = $rs
    $null = $ps.AddScript($srv.ToString()).AddArgument($B)
    $null = $ps.BeginInvoke()
    $B.PS = $ps; $B.RS = $rs
    $script:B = $B
}

function Open-UiWindow([string]$url) {
    $exe = ''
    foreach ($p in @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")) {
        if (-not $exe -and (Test-Path -LiteralPath $p)) { $exe = $p }
    }
    try {
        if ($exe) { Start-Process -FilePath $exe -ArgumentList "--app=$url --user-data-dir=`"$(Join-Path $script:StateDir 'browser')`" --no-first-run --no-default-browser-check --window-size=1200,800" }
        else { Start-Process $url }
    }
    catch { Write-Host "  could not open a browser window ($($_.Exception.Message)); open the URL above yourself" }
}

function Send-Ui($h) {
    if ($script:A -and $script:A.Sub) { $h['sub'] = $true }
    $script:B.Out.Enqueue((ConvertTo-PyJson $h $true $false $true))
}

function Wait-Ui([string]$type, [string]$id) {
    # Next page message of that type (and id); $null when the window is gone.
    while ($true) {
        $x = $null
        while ($script:B.In.TryDequeue([ref]$x)) {
            $m = $null
            try { $m = $x | ConvertFrom-Json } catch { continue }
            if ($m.type -eq $type -and (-not $id -or $m.id -eq $id)) { return $m }
        }
        if ($script:B.Bye) { return $null }
        Start-Sleep -Milliseconds 30
    }
}

function Repair-Interrupted {
    # A stop between the assistant's tool calls and their results leaves calls unanswered; answer them.
    $i = $script:Messages.Count - 1
    while ($i -ge 0 -and (Get-Prop $script:Messages[$i] 'role') -eq 'tool') { $i-- }
    if ($i -lt 0 -or (Get-Prop $script:Messages[$i] 'role') -ne 'assistant') { return }
    $done = @{}
    for ($k = $i + 1; $k -lt $script:Messages.Count; $k++) { $done['' + (Get-Prop $script:Messages[$k] 'tool_call_id')] = 1 }
    foreach ($c in @(Get-Prop $script:Messages[$i] 'tool_calls' | Where-Object { $_ })) {
        $cid = '' + (Get-Prop $c 'id')
        if (-not $done[$cid]) { $script:Messages += @{ role = 'tool'; tool_call_id = $cid; name = ('' + (Get-Prop (Get-Prop $c 'function') 'name')); content = 'Tool execution aborted' } }
    }
}

function Invoke-TurnSafe([string]$text, [bool]$retry) {
    try { Invoke-Turn $text $retry } catch { $script:TurnErr = '' + $_.Exception.Message }
}

function Invoke-GuiTurn([string]$text, [bool]$retry) {
    # The turn runs as a nested pipeline so the server thread can stop it (Esc in the page).
    if (-not $retry) { Send-Ui @{ t = 'user'; text = $text } }
    $script:TurnErr = ''
    $script:TurnEnded = $false
    $ps = [powershell]::Create([System.Management.Automation.RunspaceMode]::CurrentRunspace)
    $null = $ps.AddScript('Invoke-TurnSafe $args[0] $args[1]').AddArgument($text).AddArgument($retry)
    $script:B.Cur = $ps
    try { $null = $ps.Invoke() }
    catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; if (-not $script:TurnErr) { $script:TurnErr = '' + $e.Message } }
    finally { $script:B.Cur = $null }
    $stopped = ('' + $ps.InvocationStateInfo.State) -eq 'Stopped'
    $ps.Dispose()
    if ($stopped) { $script:TurnErr = ''; Repair-Interrupted; Clear-Wait; Save-Session; if (-not $script:B.Bye) { WL '  interrupted' 'DarkYellow' } }
    if (-not $script:TurnEnded) { Send-Ui @{ t = 'turn_end'; steps = 0; secs = 0; ctx = $script:LastCtx; ctxEst = $script:LastCtxEst; credits = $script:CreditsLeft; delta = 0; interrupted = $stopped } }
    if ($script:TurnErr) { throw $script:TurnErr }
}

function Send-UiHistory {
    Send-Ui @{ t = 'reset' }
    foreach ($m in $script:Messages) {
        $role = '' + (Get-Prop $m 'role')
        $text = '' + (Get-MessageText $m)
        if ($role -eq 'user') { Send-Ui @{ t = 'user'; text = $text } }
        elseif ($role -eq 'assistant' -and $text.Trim()) { Send-Ui @{ t = 'md'; text = $text } }
    }
}

# ---------------------------------------------------------------- main
$script:AutoApproveOn = [bool]$AutoApprove
$script:CreditsLeft = $null
$script:LastCtx = 0
$script:LastCtxEst = $false
$script:LastFinishReason = ''
$script:Mode = ''
$script:InstrFiles = @()
Reset-Conversation
$script:Shell = Find-Shell

# ---------------------------------------------------------------- UI selection
$script:GuiOn = $false
$script:CurCall = ''
$uiWant = 'auto'
if ($Gui) { $uiWant = 'gui' }
if ($Tui -or $Prompt) { $uiWant = 'tui' }
if ($uiWant -eq 'gui' -or ($uiWant -eq 'auto' -and $script:OnWindows)) {
    if ($script:LangMode -ne 'FullLanguage') {
        if ($uiWant -eq 'gui') { Say "  the GUI needs FullLanguage (this session is $($script:LangMode)); using the console UI" 'DarkYellow' }
    }
    else {
        try {
            Start-UiServer
            $uiUrl = "http://127.0.0.1:$($script:B.Port)/?t=$($script:B.Token)"
            Write-Host "GUI $uiUrl"
            Write-Host '  harness-lite runs in that window; closing it (or Ctrl+C here) quits. -Tui keeps the console UI.'
            if (-not $env:HARNESS_LITE_GUI_NOOPEN) { Open-UiWindow $uiUrl }
            $n = 0
            while (-not $script:B.Connected -and $n -lt 300) { Start-Sleep -Milliseconds 100; $n++ }
            if (-not $script:B.Connected) { $script:B.Stop = $true; throw 'no window connected within 30 s' }
            $script:GuiOn = $true
        }
        catch { $script:GuiOn = $false; Say "  the GUI could not start ($($_.Exception.Message)); using the console UI" 'DarkYellow' }
    }
}
if ($script:GuiOn) {
    # Output functions redefined to emit page events; the console UI never sees these.
    function Get-Width { return 200 }
    function Clear-Wait { if ($script:WaitShown) { $script:WaitShown = $false; Send-Ui @{ t = 'status'; text = '' } } }
    function Show-Wait([string]$label) { $script:WaitShown = $true; Send-Ui @{ t = 'status'; text = ($label -replace ', Ctrl\+C quits harness-lite', '') } }
    function W([string]$t, [string]$c) { if ($t -eq '') { return }; Clear-Wait; Send-Ui @{ t = 'out'; text = $t; color = $c; nl = $false } }
    function WL([string]$t, [string]$c) { Clear-Wait; Send-Ui @{ t = 'out'; text = $t; color = $c; nl = $true } }
    function Write-Markdown([string]$text) { Clear-Wait; Send-Ui @{ t = 'md'; text = $text } }
    function Show-ToolCall([string]$name, $a) {
        $label = $script:ToolLabels[$name]
        if (-not $label) { $label = $name }
        Clear-Wait; Send-Ui @{ t = 'tool'; id = $script:CurCall; name = $name; label = $label; detail = (Get-ToolDetail $name $a); args = $a }
    }
    function Show-Diff($oldLines, $newLines) { Send-Ui @{ t = 'diff'; old = @($oldLines); new = @($newLines) } }
    function Show-Todos {
        $items = @()
        foreach ($x in @(Get-LatestTodos | Where-Object { $null -ne $_ })) { $items += @{ content = ('' + (Get-Prop $x 'content')); status = ('' + (Get-Prop $x 'status')) } }
        Send-Ui @{ t = 'todos'; items = $items }
    }
    function Show-TurnStatus([int]$steps, $t0, $creditsBefore) {
        $delta = 0
        if ($null -ne $creditsBefore -and $null -ne $script:CreditsLeft) { $delta = $creditsBefore - $script:CreditsLeft }
        $script:TurnEnded = $true
        Send-Ui @{ t = 'turn_end'; steps = $steps; secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1); ctx = $script:LastCtx; ctxEst = [bool]$script:LastCtxEst; credits = $script:CreditsLeft; delta = $delta; interrupted = $false }
    }
    function Show-Banner {
        $models = @()
        foreach ($id in $script:CatalogIds) { $models += @{ id = $id; note = (Get-ModelNote $id) } }
        $cmds = @()
        foreach ($r in $script:Commands) { $cmds += @{ name = ($r[0] -replace '\s.*$', ''); desc = $r[1] } }
        $res = $null
        if (Test-Path -LiteralPath $script:SessionFile) { $res = @{ when = (Get-Date (Get-Item -LiteralPath $script:SessionFile).LastWriteTime -Format 'yyyy-MM-dd HH:mm') } }
        Send-Ui @{ t = 'hello'; version = $script:Version; root = $script:RootFull; shell = ('' + $script:Shell.exe); model = $script:ModelId; note = (Get-ModelNote $script:ModelId)
            mode = 'build'; auto = [bool]$script:AutoApproveOn; commands = $cmds; models = $models; resumable = $res }
    }
    function Request-Approval([string]$what) {
        if ($script:AutoApproveOn) { return @{ ok = $true; note = '' } }
        $id = [guid]::NewGuid().ToString('N')
        Send-Ui @{ t = 'ask'; id = $id; what = $what }
        $m = Wait-Ui 'answer' $id
        if ($null -eq $m) { return @{ ok = $false; note = '' } }
        return (Resolve-Approval ('' + $m.value))
    }
    function Select-Model {
        if ($script:CatalogIds.Count -eq 0) { Get-Catalog }
        $items = @()
        foreach ($id in $script:CatalogIds) { $items += @{ id = $id; note = (Get-ModelNote $id) } }
        $def = Get-LastModel
        if (-not $def -or $script:CatalogIds -notcontains $def) { $def = $script:ModelId }
        if (-not $def) { $def = $script:CatalogIds[0] }
        $id = [guid]::NewGuid().ToString('N')
        Send-Ui @{ t = 'pick'; id = $id; title = 'Select model'; items = $items; def = $def }
        $m = Wait-Ui 'pick' $id
        if ($null -eq $m) { if ($script:ModelId) { return '' }; $script:B.Stop = $true; exit 0 }
        return ('' + $m.value)
    }
    function Read-Config([string]$req, [string]$err) {
        $e = $null
        if ($err) { $e = @{ request = $req; message = $err } }
        elseif ($script:CfgWarn) { $e = @{ request = 'reading the saved config'; message = $script:CfgWarn }; $script:CfgWarn = '' }
        $id = [guid]::NewGuid().ToString('N')
        Send-Ui @{ t = 'config'; id = $id; url = ('' + $script:BaseUrl); auth = ('' + $Auth); model = ('' + $script:Cfg.model); error = $e }
        $m = Wait-Ui 'config' $id
        if ($null -eq $m) { return $null }
        $k = '' + $m.key
        if (-not $k) { $k = $script:ApiKey }
        return @{ url = ('' + $m.url); key = $k; auth = ('' + $m.auth) }
    }
}

WL ''
W ' harness-lite ' 'Cyan'; WL "$($script:G.mid) $($script:ApiRoot)" 'DarkGray'
Confirm-Config ''
if ($Model -and -not $script:ModelGiven -and $script:CatalogIds -notcontains $Model) { Say "  the saved default model $Model is not offered any more" 'DarkYellow'; $Model = '' }
if (-not $Model) { $Model = Select-Model; $script:Cfg.model = $Model; Save-Config }
Set-Model $Model
WL ''
Show-Banner
if ($Resume) { Restore-Session; if ($script:GuiOn) { Send-UiHistory } }
elseif (Test-Path -LiteralPath $script:SessionFile) {
    $when = ''
    try { $when = Get-Date (Get-Item -LiteralPath $script:SessionFile).LastWriteTime -Format 'yyyy-MM-dd HH:mm' } catch { }
    Say "  /resume continues the conversation saved here at $when" 'DarkGray'
}
if (-not $Prompt) { Say '  /help for commands' 'DarkGray' }

if ($Prompt) {
    WL ''
    W ($script:G.prompt + ' ') 'Cyan'; WL $Prompt
    try { Invoke-Turn $Prompt } catch { Clear-Wait; Say "  error: $($_.Exception.Message)" 'Red'; exit 1 }
    exit 0
}

try {
    while ($true) {
        if ($script:GuiOn) {
            $msg = Wait-Ui 'prompt' ''
            if ($null -eq $msg) { break }
            $line = '' + $msg.text
        }
        else { $line = Read-UserInput }
        if ($null -eq $line) { break }
        if (-not (Invoke-Line $line)) { break }
    }
}
finally {
    Clear-Wait
    if ($script:Messages.Count -gt 0) { Save-Session; Say '  conversation saved; /resume (or -Resume) picks it up next time' 'DarkGray' }
    if ($script:GuiOn) { Send-Ui @{ t = 'bye' }; Start-Sleep -Milliseconds 300; $script:B.Stop = $true; Start-Sleep -Milliseconds 100; try { $script:B.PS.Dispose(); $script:B.RS.Dispose() } catch { }; if ($script:B.Err) { Write-Host "  GUI server: $($script:B.Err)" } }
}
