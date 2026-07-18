# PDvAgente.md — Персональные данные пользователя в агенте (ФИО / должность / телефон)

## Цель

Пользователь открывает окно агента (из меню трея), вводит три строки — ФИО (не более 140 знаков), должность, телефон — и нажимает «Отправить»; данные сохраняются в `config.json` агента и немедленно уходят на сервер существующим `Transport`-механизмом. Данные можно менять позже из агента и из дашборда (виджет на странице устройства, через существующий `PATCH /api/v1/devices/{device_id}/meta`). В дашборде они отображаются в карточке компьютера сразу после имени машины, в одну строку, через запятую. Структурный образец всей фичи — цепочка `org_code`/`dept_code`/`comment` (`docs/superpowers/plans/2026-06-12-tray-client.md`, «Этап 3 — справочник организаций»): schema → client config → transport → server db migration → server api patch → dashboard templates → tests → CHANGELOG.

## Инварианты и ограничения (нарушать НЕЛЬЗЯ)

1. `CONTRACT_VERSION` в `shared/schema.py` НЕ бампается — новые поля только `Optional[str] = None` (образец: комментарии у `site_code`, `shared/schema.py:399-405`; CLAUDE.md §5).
2. `client/` остаётся чистым stdlib (urllib/subprocess/json/tkinter). Никаких импортов `shared/` или pydantic в клиенте — лимиты длины дублируются константами в клиенте с комментарием-ссылкой на `shared/schema.py`.
3. `|safe` в Jinja2-шаблонах НЕ используется (autoescape включён, так и остаётся).
4. Существующие `·`-подстрочники (`dev-sub2` в `_fleet_body.html`, подстрочник в `device.html:163-172`) НЕ трогаются и НЕ меняют смысл — новый текст идёт отдельно, с запятыми.
5. COALESCE-семантика ingest: пустое/None значение от агента НИКОГДА не затирает сохранённое в БД значение (в т.ч. отредактированное с дашборда). Паттерн — `ON CONFLICT ... DO UPDATE SET col = COALESCE(excluded.col, devices.col)`, как у `comment`.
6. tkinter-mainloop и Win32-цикл трея не живут в одном процессе — новое окно запускается ТОЛЬКО дочерним процессом через `subprocess.Popen(self._child("--edit-owner"), creationflags=NO_WINDOW)` (образец: `client/tray/__main__.py:159-160`).
7. Человекочитаемые подписи в UI — по-русски («ФИО», «Должность», «Телефон»); имена полей/колонок — английские (`owner_full_name`, `owner_position`, `owner_phone`).
8. Лимиты длины (единые на всех уровнях): `owner_full_name` — 140 (жёсткое требование пользователя); `owner_position` — 140 (та же природа — строка на русском, симметрия с ФИО упрощает UI и валидацию); `owner_phone` — 32 (достаточно для международного формата с добавочным, заведомо короче — образец коротких лимитов `PrintJobRecord.source`, max_length=16).
9. Валидация длины ФИО = 140 обязана существовать на трёх уровнях: pydantic (`Envelope` и `MetaPatch`), tkinter-форма (до отправки, с сообщением об ошибке), HTML (`maxlength="140"` + серверная pydantic-валидация).
10. Out of scope: никакой истории изменений персональных данных, никаких новых endpoint'ов, никаких новых msg_type — только ввод, редактирование, отображение трёх строк.
11. R4-класс задачи (CLAUDE.md §2/§6): каждая задача — строго TDD (сначала RED-тест, потом GREEN-реализация).

## Атомарные задачи (выполнять строго по порядку)

### TASK 1 — Контракт: три поля в `Envelope`

- (a) Файл: `shared/schema.py`.
- (b) Место: класс `Envelope` (строки 385-415), после блока `org_code`/`dept_code`/`comment` (строки 407-409), перед `idempotency_key`.
- (c) Добавить три поля с комментарием в стиле соседних («additive optional; COALESCE on server; CONTRACT_VERSION deliberately NOT bumped»): `owner_full_name: Optional[str] = Field(default=None, max_length=140)`, `owner_position: Optional[str] = Field(default=None, max_length=140)`, `owner_phone: Optional[str] = Field(default=None, max_length=32)`. `CONTRACT_VERSION` не менять.
- (d) RED: новый файл `tests/test_owner_identity.py` — тесты: (1) `Envelope` без новых полей валиден и все три равны `None`; (2) `Envelope` с тремя заполненными полями валиден; (3) `owner_full_name` длиной 141 символ отвергается `ValidationError`; (4) длиной ровно 140 — принимается. GREEN: поля добавлены, тесты проходят.
- (e) Готово: 4 теста зелёные, `CONTRACT_VERSION == "0.1.0"` не изменился, существующий `tests/test_contract_compat.py` проходит.

### TASK 2 — БД: колонки + миграция

- (a) Файл: `server/db.py`.
- (b) Места: `_SCHEMA`, CREATE TABLE `devices` (строки 137-157) — после строки `comment TEXT,` (строка 150); и `_ADD_COLUMNS["devices"]` (строки 549-561) — после записи `("comment", "TEXT"),`.
- (c) Добавить в оба места три колонки: `owner_full_name TEXT`, `owner_position TEXT`, `owner_phone TEXT` (в `_ADD_COLUMNS` — кортежи `("owner_full_name", "TEXT")` и т.д.). Backfill не нужен (`_BACKFILL` не трогать). Механизм `_migrate_add_columns()` (строки 594-611) менять не нужно — он подхватит записи автоматически.
- (d) RED: в `tests/test_owner_identity.py` — тест: инициализировать БД, через `PRAGMA table_info(devices)` убедиться, что три колонки присутствуют; второй тест — повторная инициализация поверх существующей БД не падает (идемпотентность). GREEN: колонки добавлены.
- (e) Готово: оба теста зелёные; `tests/test_db_append_only.py` и прочие db-тесты не сломаны.

### TASK 3 — БД: запись с COALESCE в `upsert_device` / `touch_device`

- (a) Файл: `server/db.py`.
- (b) Места: `upsert_device()` (строки 617-681) и `touch_device()` (строки 684+).
- (c) В обе функции добавить keyword-параметры `owner_full_name: Optional[str] = None`, `owner_position: Optional[str] = None`, `owner_phone: Optional[str] = None` — рядом с параметром `comment`; в SQL обеих функций добавить три колонки в INSERT-часть и в `ON CONFLICT ... DO UPDATE SET` строго по существующему COALESCE-паттерну колонки `comment` (`col = COALESCE(excluded.col, devices.col)`).
- (d) RED: тесты в `tests/test_owner_identity.py`: (1) upsert с заполненными полями сохраняет их; (2) последующий upsert/touch с `None` в этих полях НЕ затирает сохранённые значения; (3) touch_device с заполненным полем обновляет значение. GREEN: параметры и SQL добавлены.
- (e) Готово: три теста зелёные, существующие тесты upsert/touch не сломаны.

### TASK 4 — БД: чтение в `get_devices` / `get_device`

- (a) Файл: `server/db.py`.
- (b) Места: `get_devices()` — явный список колонок в SELECT (строки 2603-2607) и явная сборка словаря (строки 2636-2689), добавить рядом с `comment`; `get_device()` — только явный словарь на возврате (строки 2745-2767; SELECT там уже `SELECT *`).
- (c) Добавить ключи `owner_full_name`, `owner_position`, `owner_phone` в оба возвращаемых словаря (и в SELECT `get_devices`).
- (d) RED: тесты в `tests/test_owner_identity.py`: после upsert с полями `get_devices()` и `get_device()` возвращают словари с этими тремя ключами и правильными значениями; для устройства без данных — ключи присутствуют со значением `None`. GREEN: чтение расширено.
- (e) Готово: тесты зелёные.

### TASK 5 — БД: сеттеры для дашборда

- (a) Файл: `server/db.py`.
- (b) Место: рядом с `set_device_comment()` (строка 4828), по его точному образцу (простой UPDATE одной колонки, возврат `bool`).
- (c) Три функции: `set_device_owner_full_name(device_id: str, value: Optional[str]) -> bool`, `set_device_owner_position(device_id: str, value: Optional[str]) -> bool`, `set_device_owner_phone(device_id: str, value: Optional[str]) -> bool`. Семантика как у `set_device_comment`: прямой UPDATE (позволяет очистить значение с дашборда — это осознанная асимметрия с COALESCE-ingest, идентичная полю `comment`).
- (d) RED: тесты в `tests/test_owner_identity.py`: сеттер записывает значение; сеттер с `None`/пустой строкой очищает; для несуществующего device_id — возврат как у `set_device_comment` (посмотреть его контракт и повторить). GREEN: сеттеры написаны.
- (e) Готово: тесты зелёные, `bandit` не ругается (никакой конкатенации SQL — параметризованный UPDATE).

### TASK 6 — Ingest: проброс полей во всех ветках `ingest_envelope`

- (a) Файл: `server/pipeline.py`.
- (b) Место: `ingest_envelope()` (строки 324-459), все 7 веток по `msg_type` (`inventory` 333, `historical` 353, `heartbeat` 379, `events` 395, `print_jobs` 413, `liveness` 429, `update_status` 446) — в каждой уже есть вызов `db.upsert_device()`/`db.touch_device()` с `comment=env.comment`.
- (c) В КАЖДЫЙ из 7 вызовов добавить `owner_full_name=env.owner_full_name`, `owner_position=env.owner_position`, `owner_phone=env.owner_phone` — рядом с `comment=env.comment`.
- (d) RED: тесты в `tests/test_owner_identity.py`: (1) ingest конверта `heartbeat` с тремя полями → `get_device()` их возвращает; (2) ingest конверта `inventory` с полями → то же; (3) последующий `heartbeat` без полей (None) НЕ затирает; (4) параметризованный тест по всем 7 `msg_type`, что поле прокинуто (по образцу существующих ingest-тестов в `tests/test_ingest.py` / `tests/test_site_identity.py`). GREEN: 7 веток дополнены.
- (e) Готово: тесты зелёные, `tests/test_ingest.py`, `tests/test_trust_pipeline.py` не сломаны.

### TASK 7 — API: расширение `MetaPatch` + `patch_device_meta`

- (a) Файл: `server/api.py`.
- (b) Места: класс `MetaPatch` (строки 541-546) и функция `patch_device_meta()` (строки 549-557).
- (c) В `MetaPatch` добавить: `owner_full_name: Optional[str] = Field(default=None, max_length=140)`, `owner_position: Optional[str] = Field(default=None, max_length=140)`, `owner_phone: Optional[str] = Field(default=None, max_length=32)`. В `patch_device_meta` — три блока `if body.<field> is not None: db.set_device_<field>(device_id, body.<field>)` по точному образцу блока `comment` (строки 555-556).
- (d) RED: тесты в `tests/test_owner_identity.py` (через существующий FastAPI test client, образец — `tests/test_dashboard_api.py`): (1) PATCH с тремя полями → 200, значения читаются назад через `get_device`; (2) PATCH с `owner_full_name` из 141 символа → 422; (3) PATCH пустой строкой очищает поле; (4) PATCH на несуществующий device_id → 404. GREEN: модель и endpoint расширены.
- (e) Готово: тесты зелёные, старое поведение `comment`/`department` в PATCH не изменилось.

### TASK 8 — Клиент: поля в `ClientConfig`

- (a) Файл: `client/config.py`.
- (b) Место: dataclass `ClientConfig`, после поля `comment` (строка 64).
- (c) Три поля по образцу `comment`: `owner_full_name: str = ""`, `owner_position: str = ""`, `owner_phone: str = ""` с комментариями в том же стиле. Убедиться, что `load_config()` (строка 171) / `save_config()` (строка 196) подхватывают их существующим механизмом (dataclass-поля читаются/пишутся автоматически — если там явный список полей, дополнить его так же, как для `comment`).
- (d) RED: тесты в `tests/test_config.py` (дополнить): round-trip `save_config` → `load_config` сохраняет три поля; конфиг без этих полей загружается с пустыми строками (обратная совместимость). GREEN: поля добавлены.
- (e) Готово: тесты зелёные; в `client/` нет новых импортов вне stdlib.

### TASK 9 — Клиент: поля в конверте `Transport._envelope`

- (a) Файл: `client/transport.py`.
- (b) Место: `_envelope()` (строки 114-138), блок полей `site_code`/`comment` (строки 128-133).
- (c) Добавить рядом три ключа по точному образцу `"comment": self._cfg.comment or None`: `"owner_full_name": self._cfg.owner_full_name or None`, `"owner_position": self._cfg.owner_position or None`, `"owner_phone": self._cfg.owner_phone or None` (пустая строка → None → COALESCE на сервере не затирает).
- (d) RED: тесты в `tests/test_transport.py` (дополнить, по образцу существующих тестов `_envelope`/site_code): конверт содержит три ключа со значениями из конфига; при пустых строках в конфиге — значения `None`. GREEN: ключи добавлены.
- (e) Готово: тесты зелёные, `tests/test_transport_hardening.py` не сломан.

### TASK 10 — Клиент: tkinter-окно ввода в `panel.py`

- (a) Файл: `client/tray/panel.py`.
- (b) Место: после `run_password_prompt` (заканчивается на строке 177). Образец структуры окна — сам `run_password_prompt` (строки 125-177: `tk.Tk`, `ttk.Frame(padding=16)`, `ttk.Label`/`ttk.Entry`/`ttk.Button`, `root.bind("<Return>", ...)`, `messagebox`).
- (c) Добавить: (1) модульные константы `OWNER_FULL_NAME_MAX = 140`, `OWNER_POSITION_MAX = 140`, `OWNER_PHONE_MAX = 32` с комментарием «дублируют max_length в shared/schema.py — клиент stdlib-only и не может импортировать pydantic-схему»; (2) чистую функцию `validate_owner_fields(full_name: str, position: str, phone: str) -> Optional[str]` — возвращает русский текст ошибки при превышении любого лимита (после `.strip()`), иначе `None` — чистая, без tkinter, для headless-тестов; (3) функцию `run_owner_form(*, config_path: Path) -> int` — окно с заголовком «SRP — Персональные данные», три подписанных поля («ФИО», «Должность», «Телефон»), предзаполненные текущими значениями из `load_config(config_path)`, кнопки «Отправить» и «Отмена», Enter = отправить. Логика кнопки «Отправить» (вариант (а) — немедленная отправка): собрать значения → `validate_owner_fields`; при ошибке — `messagebox.showerror` и остаться в окне; при успехе — записать поля в конфиг и `save_config`, затем создать `Transport(cfg)` и вызвать `transport.send("heartbeat", {})` (пустой dict проходит no-op-проверку `payload is None`; конверт понесёт новые поля из конфига, серверная ветка `heartbeat` их COALESCE-ит); при `send(...) is True` — `messagebox.showinfo` «Отправлено на сервер», при `False` — `messagebox.showinfo` «Сохранено; будет отправлено при восстановлении связи» (Transport уже сам буферизует) — в обоих случаях закрыть окно, вернуть 0; «Отмена» — вернуть 1 без записи.
- (d) RED: тесты в `tests/test_tray_panel_logic.py` (дополнить, только чистая логика, без tkinter): `validate_owner_fields` — (1) все поля в лимитах → None; (2) ФИО 141 знак → строка ошибки с упоминанием 140; (3) должность/телефон сверх лимитов → ошибки; (4) пустые строки допустимы (None). GREEN: функции написаны.
- (e) Готово: тесты зелёные; сетевой вызов и `save_config` происходят только по кнопке «Отправить» после успешной валидации; окно не импортирует ничего вне stdlib.

### TASK 11 — Трей: пункт меню в `icon.py`

- (a) Файл: `client/tray/icon.py`.
- (b) Места: строка 44 (ID-константы), `TrayIcon.__init__` (строки 102-118, callbacks + карта ID→handler), `_popup_menu()` (строки 286-300).
- (c) Добавить константу `ID_OWNER = 0xE005` в строку 44; параметр `on_owner: Callable[[], None]` в `__init__` (рядом с `on_about`) и запись `ID_OWNER: on_owner` в карту обработчиков; в `_popup_menu` — `AppendMenuW(menu, MF_STRING, ID_OWNER, "Мои данные")` между пунктами «Обновить» и «О программе».
- (d) RED: тест в `tests/test_tray_icon_redraw.py` или `tests/test_tray_exit.py` (по образцу существующих тестов конструирования TrayIcon с фиктивными callbacks): создание `TrayIcon` требует `on_owner` и кладёт его в карту обработчиков под `ID_OWNER`. GREEN: параметр и пункт меню добавлены.
- (e) Готово: тест зелёный. Примечание: единственный реальный вызов конструктора `TrayIcon` (в `client/tray/__main__.py`) обновляется в TASK 12 — до его выполнения `__main__.py` временно не передаёт `on_owner`; это допустимо в рамках пошагового TDD (CLAUDE.md §0: полный гейт — один раз, перед коммитом; touched-тесты — на каждом шаге), но TASK 12 должен выполняться сразу следующим шагом, без промежуточного коммита-разрыва.

### TASK 12 — Трей: режим `--edit-owner` в `__main__.py`

- (a) Файл: `client/tray/__main__.py`.
- (b) Места: докстринг с перечнем флагов (строки 5-6), `_TrayApp.__init__` — конструктор `TrayIcon` (строки 147-152), рядом с `open_panel` (строки 159-160), `_parse_args` (строки 324-336), `main` (строки 339+).
- (c) Добавить: метод `_TrayApp.open_owner_form(self) -> None` — `subprocess.Popen(self._child("--edit-owner"), creationflags=NO_WINDOW)` c тем же `# nosec B603`-комментарием и обоснованием (фиксированный argv, без shell), по точному образцу `open_panel`; передать `on_owner=self.open_owner_form` в конструктор `TrayIcon` (замыкает разрыв из TASK 11); в `_parse_args` — `p.add_argument("--edit-owner", action="store_true", help=...)`; в `main` — ветка: при `args.edit_owner` вызвать `run_owner_form(config_path=...)` из `panel.py` и вернуть его код (по образцу веток `--panel`/`--ask-password`); дополнить докстринг файла.
- (d) RED: тест в `tests/test_tray_panel_logic.py` или `tests/test_tray_exit.py` (по образцу существующих тестов `_parse_args`/`main`-диспетчера, если есть; иначе новый тест там же): `_parse_args(["--edit-owner"])` даёт `edit_owner=True`; `main(["--edit-owner"])` вызывает `run_owner_form` (замокать) и возвращает его код. GREEN: флаг и диспетчеризация добавлены.
- (e) Готово: тесты зелёные; `bandit` чист (B603 с nosec-обоснованием как у соседей); конструктор `TrayIcon` из TASK 11 теперь везде вызывается с `on_owner`.

### TASK 13 — Дашборд: строка в карточке флота

- (a) Файл: `server/web/templates/_fleet_body.html`.
- (b) Место: строка 84, СРАЗУ после `{{ d.display_name }}</a>` и ПЕРЕД существующим `{% if d.chassis or d.model %}`-блоком. Блок `dev-sub2` (строки 85-89) не трогать.
- (c) Добавить условный `<span class="dev-sub">`, который выводится только если хотя бы одно из `d.owner_full_name` / `d.owner_position` / `d.owner_phone` непусто, и содержит непустые значения, соединённые строкой `", "` без хвостовых/двойных запятых (в Jinja: собрать список из трёх значений, отфильтровать пустые, `join(", ")` — через `{% set %}` перед строкой или inline-фильтрами). Без `|safe`. Перед owner-строкой — разделитель-запятая после имени машины: `, ` внутри того же span.
- (d) RED: тест в `tests/test_owner_identity.py` (по образцу рендер-тестов `tests/test_live_dashboard.py`/`tests/test_device_card.py`): устройство с ФИО+должность+телефон → HTML флота содержит `Иванов И.И., инженер, +7 900 000-00-00` одной строкой; устройство только с ФИО → есть ФИО, нет висячих запятых; устройство без данных → owner-span отсутствует; XSS-проба (`<b>` в ФИО) → в HTML заэкранировано. GREEN: шаблон дополнен.
- (e) Готово: тесты зелёные; вид `dev-sub2` не изменился.

### TASK 14 — Дашборд: страница устройства (отображение + редактирование)

- (a) Файл: `server/web/templates/device.html`.
- (b) Места: (1) отображение — новый элемент после `</div>` заголовочного flex-блока (строка 162) и ПЕРЕД `·`-подстрочником (строка 163), сам подстрочник не менять; (2) редактирование — новый блок после существующего виджета комментария (после `</script>` на строке 596, перед `{% endblock %}`).
- (c) Отображение: `<div>` с той же логикой «непустые из трёх, join запятой», что в TASK 13. Редактирование — по ТОЧНОМУ образцу виджета комментария (строки 542-596): `section-label` «Персональные данные»; три строки `dept-row`, каждая — `<label>` («ФИО», «Должность», «Телефон») + `<input class="dept-input">` с id `owner-name-input` (`maxlength="140"`, value из `d.owner_full_name or ''`), `owner-position-input` (`maxlength="140"`), `owner-phone-input` (`maxlength="32"`); одна кнопка `id="owner-save"` («Сохранить») + `<span id="owner-status">`; JS — IIFE по образцу comment-виджета (строки 571-596): по клику собрать три значения (`.trim() || null`), один `fetch` `PATCH /api/v1/devices/{did}/meta` с телом `{owner_full_name, owner_position, owner_phone}`, те же индикаторы `dept-ok`/`dept-err`. CSS-классы переиспользовать, новых стилей не добавлять. Без `|safe`.
- (d) RED: тесты в `tests/test_owner_identity.py` (образец — `tests/test_device_hero.py`/`tests/test_device_card.py`): страница устройства с данными содержит owner-строку через запятую под заголовком; содержит `id="owner-name-input"` с `maxlength="140"` и предзаполненным value; XSS-проба экранируется. GREEN: шаблон дополнен.
- (e) Готово: тесты зелёные; существующий comment-виджет работает без изменений.

### TASK 15 — Обязательное security-ревью

- (a) Затронуты ingest-поверхность, SQL-поверхность и агент — по CLAUDE.md §3 ревью проводит `security-reviewer` (Opus), НЕ обычный `code-reviewer`.
- (b) Место: весь диф фичи (все файлы TASK 1-14).
- (c) Запустить security-reviewer с фокусом: параметризация новых SQL (сеттеры, upsert/touch, миграция), отсутствие `|safe` и экранирование пользовательских строк в двух шаблонах, лимиты длины на всех трёх уровнях, `# nosec`-обоснования subprocess в `__main__.py`, отсутствие новых зависимостей в `client/`.
- (d) RED/GREEN: все замечания ревью либо исправлены, либо явно отклонены с обоснованием, зафиксированным в ответе ревью.
- (e) Готово: security-reviewer дал одобрение.

### TASK 16 — Финальные гейты + документация

- (a) Файлы: `CHANGELOG.md`, `CONTINUITY.md`.
- (b) `CHANGELOG.md` — секция `## [Unreleased]`; `CONTINUITY.md` — по конвенции файла.
- (c) Записать фичу в `## [Unreleased]` (Added: персональные данные владельца ПК — ввод в трее, ingest, PATCH meta, отображение во флоте и на странице устройства); обновить `CONTINUITY.md`. Затем прогнать ВСЕ гейты (см. Definition of Done).
- (d) —
- (e) Готово: все гейты зелёные, записи в обоих файлах сделаны.

## Definition of Done (обязательные гейты перед словом «готово»)

- `ruff` — чисто (server + shared + client + tests).
- `mypy` — чисто (server + shared + client).
- `bandit` — чисто (новые subprocess/SQL — только с обоснованными `# nosec` по образцу существующих).
- `pytest` — всё зелёное, coverage ≥ 80%.
- `python smoke.py` — проходит.
- `CHANGELOG.md` (`## [Unreleased]`) и `CONTINUITY.md` обновлены.
- Security-ревью через `security-reviewer` (Opus) пройдено (TASK 15).

## Дисциплина исполнения

Реализация идёт СТРОГО по TASK 1 → TASK 16, без пропусков, объединений и перестановок (кроме явно указанного в TASK 11/12 — эти два шага выполняются подряд, без промежуточного коммита). Никаких творческих решений, дополнительных полей, рефакторингов «заодно» или новых фич. Каждая задача начинается с RED-теста и заканчивается его GREEN-состоянием и критерием (e). Любая неопределённость, расхождение с указанными строками/именами или конфликт с существующим кодом — повод ОСТАНОВИТЬСЯ и переспросить у человека, а не додумывать самостоятельно.
