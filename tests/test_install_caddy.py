"""install-caddy.ps1 (план 2026-11-07 T2): разбор PS 5.1, чистые функции, пины безопасности.

Сам скрипт тесты НЕ запускают (служба, брандмауэр, UAC): функции берутся из AST и гоняются в
отдельном powershell.exe на временных файлах.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "install-caddy.ps1"
POWERSHELL = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), r"System32\WindowsPowerShell")
POWERSHELL = POWERSHELL / "v1.0" / "powershell.exe"

pytestmark = pytest.mark.skipif(not POWERSHELL.exists(), reason="нужен Windows PowerShell 5.1")

# Только перечисленные функции определяются в процессе теста; основной блок не выполняется.
_HEAD = """$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(__PS1__, [ref]$null, [ref]$null)
foreach ($st in $ast.EndBlock.Statements) {
    if ($st -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        @(__NAMES__) -contains $st.Name) { . ([scriptblock]::Create($st.Extent.Text)) }
}
function Out-Result($Value) {
    $json = ConvertTo-Json -InputObject $Value -Depth 5 -Compress
    [IO.File]::WriteAllText(__OUT__, $json, (New-Object System.Text.UTF8Encoding $false))
}
"""


def _q(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps(tmp_path: Path, names: List[str], body: str, **paths: Path) -> Any:
    out = tmp_path / "out.json"
    script = (_HEAD + body).replace("__NAMES__", ", ".join(_q(n) for n in names))
    for key, value in {"PS1": PS1, "OUT": out, **paths}.items():
        script = script.replace(f"__{key}__", _q(value))
    harness = tmp_path / "harness.ps1"
    harness.write_text(script, encoding="utf-8-sig")
    proc = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        + ["-File", str(harness)],
        capture_output=True,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr.decode("oem", "replace")
    return json.loads(out.read_text(encoding="utf-8"))


def _text() -> str:
    return PS1.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def _fn(name: str) -> str:
    match = re.search(r"^function " + re.escape(name) + r"\b.*?^}", _text(), re.S | re.M)
    assert match is not None, name
    return match.group(0)


def test_ps1_parses_in_51_and_only_defines_functions_until_the_guarded_main(
    tmp_path: Path,
) -> None:
    # Без BOM PS 5.1 читает файл в ANSI-кодировке: русские сообщения превратились бы в мусор.
    assert PS1.read_bytes().startswith(b"\xef\xbb\xbf")
    got = _ps(
        tmp_path,
        [],
        """$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(__PS1__, [ref]$tokens, [ref]$errors)
$st = $ast.EndBlock.Statements
Out-Result ([ordered]@{
    errors = @($errors | ForEach-Object { $_.Message })
    top = @($st | ForEach-Object { $_.GetType().Name })
    guard = $st[-1].Clauses[0].Item1.Extent.Text
    sizes = @($st | Where-Object { $_ -is [System.Management.Automation.Language.FunctionDefinitionAst] } |
        ForEach-Object { $_.Extent.EndLineNumber - $_.Extent.StartLineNumber + 1 })
    blocks = @($ast.BeginBlock, $ast.ProcessBlock, $ast.DynamicParamBlock) -ne $null
})
""",
    )
    assert got["errors"] == []
    # dot-source и извлечение из AST ничего не запускают: до основного блока -- только
    # $ErrorActionPreference и определения функций; основной блок -- последний, под условием.
    top = got["top"]
    assert top[0] == "AssignmentStatementAst" and top[-1] == "IfStatementAst"
    assert set(top[1:-1]) == {"FunctionDefinitionAst"}
    assert got["guard"] == '$MyInvocation.InvocationName -ne "."'
    assert got["blocks"] == []
    assert max(got["sizes"]) < 50


PUBLIC = [
    "8.8.8.8",
    "1.1.1.1",
    "85.140.12.34",
    "100.63.255.255",
    "100.128.0.1",
    "172.15.255.255",
    "172.32.0.1",
    "169.253.255.255",
    "198.20.0.1",
    "223.255.255.255",
]
NOT_PUBLIC = [
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.10",
    "127.0.0.1",
    "127.255.255.254",
    "169.254.10.20",
    "100.64.0.1",
    "100.127.255.255",
    "0.0.0.0",
    "0.1.2.3",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "203.0.113.10",  # заглушка шаблона Caddyfile
    "192.0.2.10",
    "198.51.100.7",
    "198.18.0.1",
    "192.0.0.8",
    "",
    "abc",
    "1.2.3",
    "1.2.3.4.5",
    "1.2.3.256",
    "01.2.3.4",
    " 1.2.3.4",
    "1.2.3.4 ",
    "8.8.8.8\n",
    "1.2.3.4/24",
    "0x8.8.8.8",
    "::1",
    "2001:db8::1",
    "::ffff:8.8.8.8",
    "\uff18.\uff18.\uff18.\uff18",  # полноширинные цифры: \\d в .NET их принял бы
    "\u0661.\u0661.\u0661.\u0661",
]


def test_public_ipv4_accepts_only_internet_addresses(tmp_path: Path) -> None:
    cases = PUBLIC + NOT_PUBLIC
    src = tmp_path / "ips.json"
    src.write_text(json.dumps(cases), encoding="utf-8")
    got = _ps(
        tmp_path,
        ["ConvertTo-IPv4Number", "Test-PublicIPv4"],
        """$ips = [IO.File]::ReadAllText(__SRC__, [Text.Encoding]::UTF8) | ConvertFrom-Json
Out-Result @(foreach ($ip in $ips) { [bool](Test-PublicIPv4 $ip) })
""",
        SRC=src,
    )
    assert dict(zip(cases, got)) == {ip: ip in PUBLIC for ip in cases}


def test_sha512_is_checked_against_the_sum_pinned_in_the_script_and_fails_closed(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "caddy_2.11.9_windows_amd64.zip"
    archive.write_bytes(b"caddy release archive")
    changed = tmp_path / "changed.zip"
    changed.write_bytes(b"caddy release archivE")  # другой ровно один байт
    good = hashlib.sha512(archive.read_bytes()).hexdigest()  # строчные, Get-FileHash -- заглавные
    got = _ps(
        tmp_path,
        ["Assert-Sha512"],
        """$r = [ordered]@{}
$r.ok = $(try { Assert-Sha512 __ARCHIVE__ __GOOD__; 'ok' } catch { $_.Exception.Message })
$r.changed = $(try { Assert-Sha512 __CHANGED__ __GOOD__; 'ok' } catch { $_.Exception.Message })
$r.empty = $(try { Assert-Sha512 __ARCHIVE__ ''; 'ok' } catch { $_.Exception.Message })
Out-Result $r
""",
        ARCHIVE=archive,
        CHANGED=changed,
        GOOD=Path(good),
    )
    assert got["ok"] == "ok"
    # Пустая сумма -- тоже отказ: сверять не с чем, значит не ставим.
    for case in ("changed", "empty"):
        assert "не совпал с суммой в скрипте" in got[case], case
        assert "повреждён или подменён, не устанавливаю" in got[case], case


def _render(tmp_path: Path, ip: str, log: str) -> Dict[str, Any]:
    result: Dict[str, Any] = _ps(
        tmp_path,
        ["New-CaddyConfig"],
        """$template = [IO.File]::ReadAllText(__TEMPLATE__, [Text.Encoding]::UTF8)
$lf = $template.Replace("`r`n", "`n")
$r = [ordered]@{
    crlf = New-CaddyConfig ($lf.Replace("`n", "`r`n")) __IP__ __LOG__
    lf = New-CaddyConfig $lf __IP__ __LOG__
    broken = $(try { New-CaddyConfig $lf.Replace('203.0.113.10', '1.2.3.4') __IP__ 'x'; 'ok' }
        catch { $_.Exception.Message })
}
Out-Result $r
""",
        TEMPLATE=ROOT / "Caddyfile",
        IP=Path(ip),
        LOG=Path(log),
    )
    return result


def test_caddyfile_gets_the_ip_in_both_lines_and_a_log_file_in_the_global_block(
    tmp_path: Path,
) -> None:
    log = r"C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\Caddy\caddy.log"
    got = _render(tmp_path, "85.140.12.34", log)
    text = got["crlf"]
    assert got["lf"] == text  # шаблон на сервере -- с CRLF или LF, как отдал git
    assert "203.0.113.10" not in text
    lines = text.split("\r\n")
    head = lines[lines.index("{") : lines.index("}") + 1]  # блок глобальных настроек
    slashed = "C:/Windows/ServiceProfiles/LocalService/AppData/Roaming/Caddy/caddy.log"
    assert head[1:4] == ["\tlog {", f'\t\toutput file "{slashed}"', "\t}"]
    assert "\tadmin off" in head and "\tauto_https disable_redirects" in head
    sni = [ln.split()[1] for ln in head if ln.startswith("\tdefault_sni ")]
    sites = [ln[: -len(" {")] for ln in lines if ln.endswith(" {") and not ln[0].isspace()]
    assert sni == sites == ["85.140.12.34"]
    assert "\t\t\tdisable_http_challenge" in lines and "\treverse_proxy 127.0.0.1:8001" in lines
    # Клон (git) и архив сервера (без .git) -- у каждой поставки свой способ вернуть шаблон.
    assert "git checkout -- Caddyfile" in got["broken"] and "из архива сервера" in got["broken"]


def test_rendered_caddyfile_passes_caddy_validate(tmp_path: Path) -> None:
    # Живая проверка -- там, где есть caddy.exe (SRP_CADDY_EXE или PATH); в CI его нет.
    caddy = os.environ.get("SRP_CADDY_EXE") or shutil.which("caddy")
    if not caddy or not Path(caddy).exists():
        pytest.skip("caddy.exe нет: SRP_CADDY_EXE или PATH")
    log = tmp_path / "log dir" / "caddy.log"  # пробел в пути -- путь в кавычках
    config = tmp_path / "Caddyfile"
    config.write_bytes(_render(tmp_path, "85.140.12.34", str(log))["crlf"].encode("utf-8"))
    proc = subprocess.run(
        [caddy, "validate", "--config", str(config)],
        capture_output=True,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert proc.returncode == 0, output
    assert "Valid configuration" in output


def test_certificate_ready_needs_this_ip_in_the_log_or_its_cert_in_storage(
    tmp_path: Path,
) -> None:
    got = _ps(
        tmp_path,
        ["Test-CertificateReady"],
        """$ip = '85.140.12.3'; $log = __LOG__; $storage = __STORAGE__
$r = [ordered]@{ none = Test-CertificateReady $ip $log $storage }
$line = '{"level":"info","logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"IP"}'
Set-Content -LiteralPath $log -Value $line.Replace('IP', '85.140.12.34')
$r.otherIp = Test-CertificateReady $ip $log $storage
Add-Content -LiteralPath $log -Value $line.Replace('IP', $ip)
$r.logged = Test-CertificateReady $ip $log $storage
$dir = Join-Path $storage 'certificates\\acme-v02.api.letsencrypt.org-directory\\85.140.12.34'
New-Item -ItemType Directory -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir '85.140.12.34.crt') -Value 'x'
$r.otherCert = Test-CertificateReady $ip 'missing.log' $storage
$dir = Join-Path $storage 'certificates\\acme-v02.api.letsencrypt.org-directory\\85.140.12.3'
New-Item -ItemType Directory -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir '85.140.12.3.crt') -Value 'x'
$r.stored = Test-CertificateReady $ip 'missing.log' $storage
Out-Result $r
""",
        LOG=tmp_path / "caddy.log",
        STORAGE=tmp_path / "storage",
    )
    assert got == {
        "none": False,
        "otherIp": False,
        "logged": True,
        "otherCert": False,
        "stored": True,
    }


def test_wait_certificate_reads_the_log_and_the_certificates_from_one_service_folder(
    tmp_path: Path,
) -> None:
    logged, stored = tmp_path / "logged", tmp_path / "stored"
    logged.mkdir()
    line = '{"msg":"certificate obtained successfully","identifier":"85.140.12.34"}\n'
    (logged / "caddy.log").write_text(line, encoding="utf-8")
    cert = stored / "certificates" / "acme-v02.api.letsencrypt.org-directory" / "85.140.12.34"
    cert.mkdir(parents=True)
    (cert / "85.140.12.34.crt").write_text("x", encoding="ascii")
    got = _ps(
        tmp_path,
        ["Write-Head", "Write-Ok", "Test-CertificateReady", "Wait-Certificate"],
        # Сертификат найден с первой проверки -- до паузы цикла дело не доходит.
        """function Start-Sleep { throw 'нет сертификата -- ждал бы' }
$r = [ordered]@{}
$r.logged = $(try { Wait-Certificate '85.140.12.34' __LOGGED__ } catch { $_.Exception.Message })
$r.stored = $(try { Wait-Certificate '85.140.12.34' __STORED__ } catch { $_.Exception.Message })
Out-Result $r
""",
        LOGGED=logged,
        STORED=stored,
    )
    assert got == {"logged": True, "stored": True}


def test_download_is_tls12_and_checked_against_the_pinned_sum_before_unpacking() -> None:
    get = _fn("Get-CaddyBinary")
    assert get.index("[Net.SecurityProtocolType]::Tls12") < get.index("Invoke-WebRequest")
    # Сумма -- в самом скрипте: файл сумм того же релиза подменили бы вместе с архивом.
    assert re.search(r'\n    \$sha512 = "[0-9a-f]{128}"\n', get)
    assert get.index("Assert-Sha512 $zip $sha512") < get.index("Expand-Archive")
    assert get.count("Invoke-WebRequest -UseBasicParsing") == 2
    assert "checksums" not in _text().lower()
    assert '"https://github.com/caddyserver/caddy/releases/download/v2.11.9/"' in get
    # 32-битная сборка -- последняя и без суммы, и об этом сказано прямо.
    x86 = get[: get.index("$base = ")]
    assert '"https://caddyserver.com/api/download?os=windows&arch=386"' in x86
    assert "(версию там не закрепить), БЕЗ проверки контрольной суммы" in x86
    assert "-Algorithm SHA512" in _fn("Assert-Sha512")


def test_service_runs_as_localservice_and_never_stays_half_installed() -> None:
    svc = _fn("Install-CaddyService")
    assert "Invoke-CimMethod -ClassName Win32_Service -MethodName Create" in svc
    for arg in (
        'StartName = "NT AUTHORITY\\LocalService"; StartPassword = ""',
        'StartMode = "Automatic"',
        "PathName = '\"' + $Exe + '\" run --config \"' + $Config + '\"'",
        '"failure", "caddy", "reset=", "86400", "actions=", "restart/60000"',
    ):
        assert arg in svc, arg
    created, rest = svc.split("try {", 1)
    assert "Start-Service -Name caddy" in rest and "Remove-CaddyService" in rest.split("catch {")[1]
    assert "LocalSystem" not in _text()
    # Прежняя служба снимается до замены caddy.exe (иначе файл занят), но после проверок.
    main = _fn("Install-JSpaceCaddy")
    # Версию называет сам caddy.exe: 32-битная сборка -- последняя, не обязательно 2.11.9.
    version = '\n        Write-Ok ("Caddy " + (Invoke-Tool $staged @("version")).Split(" ")[0])'
    # Сам вызов caddy validate с начала строки, а не комментарий или Write-Ok о нём.
    validate = '\n        Invoke-Tool $staged @("validate", "--config", $check)'
    order = ["Get-CaddyBinary", version, validate, "Remove-CaddyService", "Copy-Item"]
    assert [main.index(s) for s in order] == sorted(main.index(s) for s in order)
    assert '\n        Write-Head "Caddy"\n' in main
    assert main.index("Set-FirewallRule") < main.index("Install-CaddyService")
    # 16 -- служба уже помечена на удаление (повторный запуск при открытом окне «Службы»):
    # не отказ, а то же ожидание ниже с подсказкой закрыть окно.
    remove = _fn("Remove-CaddyService")
    marked = remove.index("$deleted.ReturnValue -notin 0, 16")
    assert marked < remove.index("закройте окно «Службы»")


def test_service_log_lives_in_the_service_folder_not_in_programdata() -> None:
    # %ProgramData%\Caddy обычный пользователь мог создать заранее и держать в ней свой
    # caddy.log. Папка профиля LocalService -- только у SYSTEM, LocalService и
    # Администраторов, создаёт её сама служба: установщик в ней ничего не создаёт и не правит.
    text = _text()
    assert "programdata" not in text.lower()
    assert "Set-LogFolder" not in text and "icacls" not in text
    folder = 'Join-Path $env:SystemRoot "ServiceProfiles\\LocalService\\AppData\\Roaming\\Caddy"'
    assert text.count(folder) == 1  # журнал и хранилище сертификатов -- одна папка
    # И установщик берёт журнал и сертификат именно оттуда, а не из папки, которую создаст
    # кто угодно (подмена пути в основном блоке прошла бы проверки выше).
    main = _fn("Install-JSpaceCaddy")
    assert "$svcDir = " + folder in main
    assert '$log = Join-Path $svcDir "caddy.log"' in main
    assert "(New-CaddyConfig $template $ip $log)" in main
    assert "Wait-Certificate $ip $svcDir" in main


def test_system_tools_by_full_path_and_the_firewall_rule() -> None:
    text = _text()
    for tool in ("netsh", "sc"):
        assert f'${tool} = Join-Path $env:SystemRoot "System32\\{tool}.exe"' in text, tool
    bare = (
        r"""(?im)^\s*(&\s*)?["']?(netsh|sc|icacls|powershell)(\.exe)?["']?\s|Invoke-Tool\s+["']"""
    )
    assert not re.search(bare, text)
    assert '"System32\\WindowsPowerShell\\v1.0\\powershell.exe"' in text  # перезапуск с UAC
    rule = _fn("Set-FirewallRule")
    assert '"name=J-Space Caddy 443"' in rule
    assert rule.index('"delete", "rule"') < rule.index('"add", "rule"')
    assert '"localport=443", ("program=" + $Exe)' in rule


def test_invoke_tool_fails_on_the_exit_code_not_on_a_stderr_line(tmp_path: Path) -> None:
    # Строка stderr нативной программы при "Stop" в PS 5.1 сама стала бы исключением -- у
    # netsh/sc/caddy это предупреждение, а не сбой; сбой -- ненулевой код возврата.
    got = _ps(
        tmp_path,
        ["Invoke-Tool"],
        """$cmd = Join-Path $env:SystemRoot 'System32\\cmd.exe'
$r = [ordered]@{}
$r.exit3 = $(try { Invoke-Tool $cmd @('/c', 'exit', '3'); 'ok' } catch { $_.Exception.Message })
$r.stderr = $(try { Invoke-Tool $cmd @('/c', 'echo stderr-line 1>&2') }
    catch { 'исключение: ' + $_.Exception.Message })
Out-Result $r
""",
    )
    assert "cmd.exe /c exit 3 -- код 3" in got["exit3"]
    assert got["stderr"].strip() == "stderr-line"


def test_relaunched_window_without_admin_rights_stops_instead_of_relaunching_again() -> None:
    # С выключенным UAC RunAs прав не даёт: перезапущенное окно без прав перезапускало бы
    # себя снова, без конца. Оно объясняет причину красным, ждёт Enter и выходит с кодом 1.
    text = _text()
    branch = text[text.index("IsInRole(") : text.index("Set-Location -LiteralPath")]
    assert "if ($Relaunched) {" in branch
    guard = branch.index("if ($Relaunched) {")
    stop = branch.index("\n            exit 1\n", guard)
    assert guard < branch.index("Read-Host", guard) < stop < branch.index("-Verb RunAs")
    assert (
        "-ForegroundColor Red" in branch[guard:stop] and "install-caddy.bat" in branch[guard:stop]
    )


def test_bat_runs_the_ps1_by_full_path_and_keeps_the_window() -> None:
    lines = (ROOT / "install-caddy.bat").read_bytes().decode("ascii").splitlines()
    code = [ln for ln in lines if not ln.lower().startswith(("rem", "@echo off"))]
    assert code == [
        'cd /d "%~dp0"',
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile '
        '-ExecutionPolicy Bypass -File "%~dp0install-caddy.ps1"',
        "pause",
    ]
