<#
.SYNOPSIS
    Caddy для просмотра дашборда J-Space из интернета -- одним запуском.

.DESCRIPTION
    Запускать двойным щелчком по install-caddy.bat: без прав администратора скрипт сам
    попросит их (UAC) и продолжит в новом окне. Повторный запуск заменяет службу,
    правило брандмауэра и файлы в %ProgramFiles%\Caddy.

      1. Порт 443 свободен (или его держит наша служба caddy -- её заменим).
      2. Внешний IP: только публичный IPv4.
      3. Caddy -- во временную папку. 64-битная Windows: 2.11.9 из релиза GitHub, SHA-512
         архива сверяется с суммой в этом скрипте ДО распаковки. 32-битная: последняя сборка
         с caddyserver.com (версию там не закрепить), БЕЗ проверки контрольной суммы (её там
         нет), только HTTPS.
      4. Caddyfile из шаблона рядом со скриптом: IP в обе строки + журнал службы caddy.log в
         её папке %SystemRoot%\ServiceProfiles\LocalService\AppData\Roaming\Caddy (там же её
         сертификаты; доступ только у SYSTEM, LocalService и Администраторов, создаёт папку
         сама служба); caddy validate. Сбой до этого места систему не меняет.
      5. Прежняя служба caddy останавливается и удаляется; caddy.exe и Caddyfile -- в
         %ProgramFiles%\Caddy (менять их может только администратор).
      6. Брандмауэр: правило «J-Space Caddy 443» (вход TCP 443 для caddy.exe).
      7. Служба caddy: NT AUTHORITY\LocalService, автозапуск, перезапуск через минуту при
         сбое. Не запустилась -- удаляется: частичной службы не остаётся.
      8. Сертификат Let's Encrypt на IP -- ждём до 3 минут.
      9. Первая учётная запись (python -m server.users add) и итог.

    Функции не трогают систему при загрузке: основной блок внизу выполняется только при
    запуске файла, не при dot-source (тесты берут функции из AST).
#>
param(
    # Служебный: так скрипт перезапускает сам себя с правами администратора.
    [switch]$Relaunched
)

$ErrorActionPreference = "Stop"

function Write-Head($text) { Write-Host ""; Write-Host ("== " + $text) -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host ("  [ok]   " + $text) -ForegroundColor Green }
function Write-Info($text) { Write-Host ("         " + $text) -ForegroundColor Gray }
function Write-Warn2($text) { Write-Host ("  [!]    " + $text) -ForegroundColor Yellow }

function Invoke-Tool([string]$Exe, [string[]]$Arguments) {
    # Как в update.ps1: при "Stop" строка stderr нативной программы в 5.1 -- терминирующая
    # ошибка, поэтому внутри -- Continue; текст ErrorRecord берём из Exception.Message.
    $ErrorActionPreference = "Continue"
    $text = (@(& $Exe @Arguments 2>&1) | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { [string]$_.Exception.Message }
            else { [string]$_ }
        }) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) {
        throw ((Split-Path -Leaf $Exe) + " " + ($Arguments -join " ") + " -- код " + $LASTEXITCODE +
            [Environment]::NewLine + $text)
    }
    return $text
}

function ConvertTo-IPv4Number([string]$Text) {
    # Строго четыре десятичных октета без ведущих нулей: [Net.IPAddress]::TryParse принял бы
    # и «1.2», и «0x7f.1», а \d в .NET -- цифры любого алфавита.
    $octet = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
    if ($Text -notmatch ("^" + $octet + "(\." + $octet + "){3}\z")) { return $null }
    $number = [long]0
    foreach ($part in $Text.Split(".")) { $number = $number * 256 + [int]$part }
    return $number
}

function Test-PublicIPv4([string]$Text) {
    $ip = ConvertTo-IPv4Number $Text
    if ($null -eq $ip) { return $false }
    # Не из интернета: 0/8, RFC1918, 100.64/10 (CGNAT провайдера), 127/8, 169.254/16,
    # служебные и для документации (RFC 5737, в т.ч. заглушка шаблона 203.0.113.10) и 224/3 --
    # multicast и резерв вплоть до 255.255.255.255.
    $nets = @("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3")
    foreach ($net in $nets) {
        $base, $bits = $net.Split("/")
        $shift = 32 - [int]$bits
        if (($ip -shr $shift) -eq ((ConvertTo-IPv4Number $base) -shr $shift)) { return $false }
    }
    return $true
}

function Assert-Sha512([string]$Path, [string]$Expected) {
    # -ne без учёта регистра: Get-FileHash даёт заглавные буквы. Пустая сумма -- тоже отказ.
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA512).Hash
    if (-not $Expected -or $actual -ne $Expected) {
        throw ("SHA-512 " + (Split-Path -Leaf $Path) +
            " не совпал с суммой в скрипте -- архив повреждён или подменён, не устанавливаю")
    }
}

function New-CaddyConfig([string]$Template, [string]$IP, [string]$LogPath) {
    # Заглушка шаблона (RFC 5737) стоит в двух строках: default_sni (браузер не шлёт SNI для
    # IP, а за пробросом Caddy видит свой локальный адрес) и адрес сайта.
    $placeholder = "203.0.113.10"
    $lines = @($Template -split "`r?`n")
    $open = [array]::IndexOf(@($lines | ForEach-Object { $_.Trim() }), "{")
    if (@($lines | Where-Object { $_.Contains($placeholder) }).Count -ne 2 -or $open -lt 0) {
        throw ("Caddyfile рядом со скриптом изменён (нужны блок { } в начале и две строки с " +
            $placeholder + ") -- верните шаблон: git checkout -- Caddyfile или Caddyfile из архива сервера")
    }
    # Окна у службы нет -- журнал в файл, в блок глобальных настроек. Косые -- прямые:
    # обратная косая в Caddyfile экранирует.
    $log = @("`tlog {", ("`t`toutput file `"" + $LogPath.Replace("\", "/") + "`""), "`t}")
    $out = @($lines[0..$open]) + $log + @($lines | Select-Object -Skip ($open + 1))
    return ($out -join "`r`n").Replace($placeholder, $IP)
}

function Assert-Port443Free {
    $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='caddy'"
    foreach ($listener in @(Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue)) {
        if ($svc -and $listener.OwningProcess -eq $svc.ProcessId) { continue }  # наша служба: заменим
        $holder = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        throw ("порт 443 занят: " + $holder.ProcessName + " (PID " + $listener.OwningProcess +
            ") -- освободите его (IIS, другой веб-сервер) и запустите снова")
    }
}

function Read-PublicIP {
    Write-Info "статический внешний IP от провайдера (виден в настройках роутера, раздел WAN)"
    while ($true) {
        $ip = ([string](Read-Host "Внешний IP сервера")).Trim()
        if (Test-PublicIPv4 $ip) { return $ip }
        Write-Warn2 ("нужен публичный IPv4, например 85.140.12.34 -- не 10.x, 172.16-31.x, 192.168.x, " +
            "100.64-127.x, 127.x, 169.254.x")
    }
}

function Get-CaddyBinary([string]$Tmp) {
    # PS 5.1 по умолчанию предлагает TLS 1.0/1.1, а GitHub и caddyserver.com принимают 1.2+.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"  # индикатор в 5.1 замедляет загрузку в разы
    $exe = Join-Path $Tmp "caddy.exe"
    if (-not [Environment]::Is64BitOperatingSystem) {
        # На GitHub 32-битной сборки нет; caddyserver.com собирает её сам: последнюю версию, без сумм.
        Write-Warn2 "32-битная Windows: Caddy -- последняя сборка с caddyserver.com (версию там не закрепить), БЕЗ проверки контрольной суммы (её там нет), только HTTPS"
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 -Uri "https://caddyserver.com/api/download?os=windows&arch=386" -OutFile $exe
        return $exe
    }
    # Сумма закреплена здесь, а не берётся из файла сумм того же релиза: тот подменили бы вместе
    # с архивом (целостность без подлинности). Новая версия Caddy = версия и сумма -- вместе.
    $base = "https://github.com/caddyserver/caddy/releases/download/v2.11.9/"
    $name = "caddy_2.11.9_windows_amd64.zip"
    $sha512 = "98753d5d42650383d00619ac70917a217141f9e92314185bf6c1b8b439cfcfdf8393da0ebb4675c8cb3b23d29a19aabe99dc5aca64fa1bfc3a1ba22509b06871"
    $zip = Join-Path $Tmp $name
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 -Uri ($base + $name) -OutFile $zip
    Assert-Sha512 $zip $sha512
    Write-Ok "SHA-512 архива совпал с суммой в скрипте"
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $Tmp "zip")
    Move-Item -LiteralPath (Join-Path $Tmp "zip\caddy.exe") -Destination $exe
    return $exe
}

function Set-FirewallRule([string]$Exe) {
    $netsh = Join-Path $env:SystemRoot "System32\netsh.exe"
    $name = "name=J-Space Caddy 443"
    # Прежнее правило с тем же именем заменяем; нет его -- netsh отвечает кодом 1, не ошибка.
    try { Invoke-Tool $netsh @("advfirewall", "firewall", "delete", "rule", $name) | Out-Null } catch { }
    # Проверка Let's Encrypt (TLS-ALPN) приходит из интернета на 443 -- правило нужно до запуска.
    Invoke-Tool $netsh @("advfirewall", "firewall", "add", "rule", $name, "dir=in", "action=allow",
        "protocol=TCP", "localport=443", ("program=" + $Exe), "enable=yes") | Out-Null
    Write-Ok "правило брандмауэра «J-Space Caddy 443»: вход TCP 443 для caddy.exe"
}

function Remove-CaddyService {
    $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='caddy'"
    if (-not $svc) { return }
    Write-Info "прежняя служба caddy: останавливаю и удаляю"
    if ($svc.State -ne "Stopped") {
        Stop-Service -Name caddy -Force
        # caddy.exe занят, пока процесс службы не вышел, а Stop-Service возвращается раньше.
        if ($svc.ProcessId) { Wait-Process -Id $svc.ProcessId -Timeout 30 -ErrorAction SilentlyContinue }
    }
    $deleted = Invoke-CimMethod -InputObject $svc -MethodName Delete
    # 16 -- служба уже помечена на удаление (повторный запуск, пока её держит, например, окно
    # «Службы»): не отказ, а то же ожидание ниже с подсказкой закрыть окно.
    if ($deleted.ReturnValue -notin 0, 16) { throw ("служба caddy не удалена: Win32_Service.Delete вернул " + $deleted.ReturnValue) }
    for ($i = 0; $i -lt 20 -and (Get-Service -Name caddy -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
    if (Get-Service -Name caddy -ErrorAction SilentlyContinue) {
        throw "служба caddy ещё удаляется -- закройте окно «Службы» (или перезагрузите компьютер) и запустите снова"
    }
}

function Install-CaddyService([string]$Exe, [string]$Config, [string]$Log) {
    # Через CIM, а не sc.exe create: кавычки внутри binPath PowerShell 5.1 передаёт нативной
    # программе неверно, а путь с пробелом без кавычек -- дыра (C:\Program.exe).
    $created = Invoke-CimMethod -ClassName Win32_Service -MethodName Create -Arguments @{
        Name = "caddy"; DisplayName = "Caddy (J-Space)"
        PathName = '"' + $Exe + '" run --config "' + $Config + '"'
        ServiceType = [byte]16; ErrorControl = [byte]1; StartMode = "Automatic"; DesktopInteract = $false
        StartName = "NT AUTHORITY\LocalService"; StartPassword = ""
    }
    if ($created.ReturnValue -ne 0) { throw ("служба caddy не создана: Win32_Service.Create вернул " + $created.ReturnValue) }
    try {
        $sc = Join-Path $env:SystemRoot "System32\sc.exe"
        Invoke-Tool $sc @("failure", "caddy", "reset=", "86400", "actions=", "restart/60000") | Out-Null
        Start-Service -Name caddy
    } catch {
        # Без частичной службы: созданная, но не запущенная служба удаляется.
        Write-Warn2 ("служба caddy не запустилась: " + $_.Exception.Message)
        Remove-CaddyService
        throw ("служба caddy удалена; подробности -- в " + $Log)
    }
    Write-Ok "служба caddy запущена: NT AUTHORITY\LocalService, автозапуск, перезапуск через минуту при сбое"
}

function Test-CertificateReady([string]$IP, [string]$Log, [string]$Storage) {
    # Сертификат уже в хранилище службы: при повторном запуске Caddy берёт его оттуда, и строки
    # о выдаче в журнале не будет. Путь известен -- без -Recurse по папке службы.
    if (Test-Path -Path (Join-Path $Storage ("certificates\*\" + $IP + "\" + $IP + ".crt"))) { return $true }
    if (-not (Test-Path -LiteralPath $Log)) { return $false }
    # Строка о выдаче -- именно для этого IP: в журнале мог остаться прежний («"IP"» в кавычках,
    # чтобы 85.140.12.3 не совпал с 85.140.12.34).
    $hits = @(Select-String -LiteralPath $Log -SimpleMatch -Pattern "certificate obtained successfully")
    return [bool](@($hits | Where-Object { $_.Line.Contains('"' + $IP + '"') }).Count)
}

function Wait-Certificate([string]$IP, [string]$Dir) {
    # Журнал и хранилище сертификатов -- одна папка службы.
    $log = Join-Path $Dir "caddy.log"
    Write-Head ("Сертификат Let's Encrypt на " + $IP + " (до 3 минут)")
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline) {
        if (Test-CertificateReady $IP $log $Dir) { Write-Ok "сертификат получен"; return $true }
        Start-Sleep -Seconds 5
    }
    Write-Warn2 ("за 3 минуты сертификата нет. Последние строки " + $log + ":")
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 20 | ForEach-Object { Write-Info $_ } }
    Write-Info "Служба продолжит попытки сама. Чаще всего ещё нет проброса 443/TCP на роутере."
    Write-Info "Запасные пути -- «Просмотр из интернета» в docs\agent-install.md (бесплатная версия --"
    Write-Info "   README проекта на GitHub): 1) ошибки tls-alpn-01 -- проброс и правило брандмауэра"
    Write-Info "   для 80/TCP, убрать строку disable_http_challenge; 2) DNS-имя вместо IP; 3) tls internal"
    Write-Info "   -- только прочитав там предупреждение о корневом сертификате. После правки"
    Write-Info "   C:\Program Files\Caddy\Caddyfile -- sc.exe stop caddy, затем sc.exe start caddy."
    return $false
}

function New-FirstAccount {
    Write-Head "Учётная запись для входа из интернета"
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $found = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $found) { Write-Warn2 "Python не найден -- запись создайте позже: users.bat add <имя>"; return }
        $python = $found.Source
    }
    $name = ([string](Read-Host "Имя записи, например ivan (Enter -- пропустить)")).Trim()
    if (-not $name) { Write-Info "пропущено -- запись позже: users.bat add <имя>"; return }
    # Вывод -- прямо в консоль: пароль печатается один раз.
    & $python -m server.users add $name
    if ($LASTEXITCODE -eq 1) {
        Write-Warn2 ("запись не создана (см. выше). Если «" + $name + "» уже есть -- новый пароль: users.bat passwd " + $name)
    } elseif ($LASTEXITCODE -ne 0) {
        Write-Warn2 ("запись не создана (код " + $LASTEXITCODE + ") -- повторите позже: users.bat add " + $name)
    }
}

function Show-Summary([string]$IP, [bool]$CertOk) {
    Write-Host ""
    Write-Host "======================================================================" -ForegroundColor Green
    Write-Host (" Caddy установлен: дашборд из интернета -- https://" + $IP) -ForegroundColor Green
    Write-Host "======================================================================" -ForegroundColor Green
    if (-not $CertOk) { Write-Warn2 "сертификата ещё нет: служба получит его сама, когда заработает проброс 443" }
    Write-Info ("Проверка с телефона (мобильный интернет, Wi-Fi выключен): https://" + $IP)
    Write-Info "  -- форма входа без предупреждения о сертификате."
    Write-Info "Роутер: проброс 443/TCP на этот компьютер (8000 для агентов -- как раньше)."
    Write-Info "Записи и пароли: users.bat add <имя>, users.bat passwd <имя>."
}

function Install-JSpaceCaddy {
    Write-Head "Порт 443"
    Assert-Port443Free
    Write-Ok "свободен (или занят нашей службой caddy)"
    Write-Head "Внешний IP"
    $ip = Read-PublicIP
    $dir = Join-Path $env:ProgramFiles "Caddy"
    # Папка службы: хранилище сертификатов Caddy под LocalService и журнал. Создаёт её сама
    # служба, доступ -- только у SYSTEM, LocalService и Администраторов: заранее не подменить.
    $svcDir = Join-Path $env:SystemRoot "ServiceProfiles\LocalService\AppData\Roaming\Caddy"
    $log = Join-Path $svcDir "caddy.log"
    $template = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "Caddyfile"), [Text.Encoding]::UTF8)
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $tmp = Join-Path $env:TEMP ("jspace-caddy-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Write-Head "Caddy"
        $staged = Get-CaddyBinary $tmp
        Write-Ok ("Caddy " + (Invoke-Tool $staged @("version")).Split(" ")[0])
        # Проверка -- с журналом во временной папке: caddy validate пишет в журнал, а в папку
        # службы от имени администратора не пишем ничего.
        $check = Join-Path $tmp "Caddyfile"
        [IO.File]::WriteAllText($check, (New-CaddyConfig $template $ip (Join-Path $tmp "validate.log")), $utf8)
        Invoke-Tool $staged @("validate", "--config", $check) | Out-Null
        Write-Ok "Caddyfile проверен (caddy validate)"
        Remove-CaddyService
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -LiteralPath $staged -Destination (Join-Path $dir "caddy.exe") -Force
        [IO.File]::WriteAllText((Join-Path $dir "Caddyfile"), (New-CaddyConfig $template $ip $log), $utf8)
        Write-Ok ("caddy.exe и Caddyfile -- в " + $dir)
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Head "Брандмауэр и служба"
    Set-FirewallRule (Join-Path $dir "caddy.exe")
    Install-CaddyService (Join-Path $dir "caddy.exe") (Join-Path $dir "Caddyfile") $log
    $certOk = Wait-Certificate $ip $svcDir
    New-FirstAccount
    Show-Summary $ip $certOk
}

# --- Основной блок: только при запуске файла, не при dot-source -------------------------
if ($MyInvocation.InvocationName -ne ".") {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        # Уже перезапущены, а прав нет: при выключенном UAC RunAs их не даёт, и новый
        # перезапуск открывал бы окна без конца.
        if ($Relaunched) {
            Write-Host ("Окно администратора открылось без прав администратора (выключен UAC?). " +
                "Войдите в Windows под учётной записью администратора и запустите install-caddy.bat снова.") -ForegroundColor Red
            Read-Host "Нажмите Enter, чтобы закрыть" | Out-Null
            exit 1
        }
        Write-Host "Нужны права администратора -- Windows сейчас спросит разрешение."
        try {
            $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
            # Start-Process склеивает аргументы через пробел без кавычек -- путь в кавычках сами.
            $child = Start-Process -FilePath $ps -Verb RunAs -Wait -PassThru -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $PSCommandPath + '"'), "-Relaunched")
            exit $child.ExitCode
        } catch {
            Write-Host ("Без прав администратора Caddy не поставить: " + $_.Exception.Message) -ForegroundColor Red
            exit 1
        }
    }
    # Запуск от имени администратора начинается в System32, а python -m server.users ищет
    # пакет server в текущей папке.
    Set-Location -LiteralPath $PSScriptRoot
    $code = 0
    try {
        Install-JSpaceCaddy
    } catch {
        Write-Host ""
        Write-Host ("Установка прервана: " + $_.Exception.Message) -ForegroundColor Red
        $code = 1
    }
    # Перезапущенное окно администратора закроется само -- даём прочитать итог.
    if ($Relaunched) { Read-Host "Нажмите Enter, чтобы закрыть" | Out-Null }
    exit $code
}
