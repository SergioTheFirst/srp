# Печать: авторежим events⇄counter (fallback без журнала)

**Решение (2026-06-12, спека tray-client §5):** режим учёта печати выбирается КАЖДЫЙ
sweep, без поля в конфиге: журнал `PrintService/Operational` включён
(`(Get-WinEvent -ListLog …).IsEnabled` → bool, локале-безопасно) → существующий
Event 307; выключен → fallback: CIM `Win32_PerfFormattedData_Spooler_PrintQueue`,
`TotalPagesPrinted` по очередям, агент хранит baseline в print_state.json и шлёт
ДЕЛЬТЫ; `current < baseline` = перезапуск спулера → baseline=current (потеря
ограничена, двойного счёта нет). Переходы: events→counter baseline=current (без
ретро); counter→events last_sweep_ts=now. Строки counter: job_id=null,
user_name=null, `source="counter"` (контракт additive, БЕЗ bump CONTRACT_VERSION;
серверный dedup UNIQUE(device_id,job_id) WHERE job_id IS NOT NULL их не трогает).

**Почему:** журнал печати может быть запрещён GPO/сломан — установщик включает его
только best-effort; самовосстановление на точный режим происходит без участия
человека, потому что решение принимает агент в рантайме, а не установщик однажды.
Счётчик спулера соответствует инварианту «CIM Win32_PerfFormattedData_*».

**Честность:** counter-режим без пользователя/документов; оба режима считают
страницы, ОТПРАВЛЕННЫЕ на принтер (физический выход подтверждает только SNMP самого
принтера — сознательно не в клиенте). Связано: [[language-independence]].
