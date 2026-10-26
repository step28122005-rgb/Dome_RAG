"""Control forms, parameter validation and observable command results."""

import math


LABELS = {
    "turn_on":"Включить", "turn_off":"Выключить", "reconnect":"Переподключить узел",
    "line_on":"Включить линию", "line_off":"Отключить линию", "reset_protection":"Взвести защиту линии",
    "reset_line_error":"Сбросить ошибку линии", "set_speed":"Задать скорость", "set_mode":"Выбрать режим",
    "reset_alarm":"Сбросить тревогу", "reset_error":"Сбросить ошибку", "home":"Найти HOME",
    "start_job":"Запустить задание", "resume":"Продолжить задание", "move":"Переместить оси",
    "set_zero":"Установить рабочий ноль", "probe_z":"Пробинг Z", "list_files":"Получить список файлов",
    "delete_file":"Удалить файл", "upload_file":"Загрузить G-code организаторов", "preheat":"Подогреть дизель",
    "enable_mppt":"Включить MPPT", "disable_mppt":"Отключить MPPT", "enable_mppt_1":"Включить MPPT 1",
    "disable_mppt_1":"Отключить MPPT 1", "enable_all_mppt":"Включить все MPPT", "disable_all_mppt":"Отключить все MPPT",
    "set_fan":"Режим вентилятора", "set_mode_hybrid":"Гибридное питание", "set_mode_grid_only":"Питание от сети",
    "start_cell_balancing":"Начать балансировку АКБ", "cancel_balancing":"Остановить балансировку",
    "transfer_fuel":"Перекачать топливо", "set_fan_pwm":"Настроить вентилятор", "set_lock":"Управление замком",
    "set_vacuum":"Пылеудаление", "set_poll_interval":"Период опроса датчиков", "set_relay_fan_level":"Уровень вытяжки",
    "set_power_limit":"Ограничение мощности", "set_threshold":"Порог сейсмодатчика", "set_thresholds":"Пороги тревоги",
    "seal_zone":"Герметизировать зону", "open_valves":"Открыть клапаны", "close_valves":"Закрыть клапаны",
    "lock_door":"Запереть дверь", "unlock_door":"Отпереть дверь", "lockdown":"Блокировка дверей",
    "manual_start_zone":"Запустить пожаротушение зоны", "stop":"Остановить пожаротушение",
    "block_automation":"Отключить автопуск пожаротушения", "unblock_automation":"Включить автопуск пожаротушения",
    "switch_to_reserve":"Переключить фильтр", "force_flush":"Промыть фильтр", "reset_warning":"Снять предупреждение",
    "recalibrate":"Перекалибровать датчик", "set_frequency":"Задать частоту", "set_protocol":"Выбрать протокол",
    "switch_channel":"Переключить канал", "set_traffic_priority":"Приоритет связи", "restart_service":"Перезапустить службу",
    "limit_load":"Ограничить нагрузку", "sync_time":"Синхронизировать время", "reboot":"Перезагрузить узел",
    "acknowledge":"Квитировать событие", "reset_interlock":"Перевзвести интерлок", "block_interlock":"Блокировать интерлок",
    "unblock_interlock":"Разблокировать интерлок", "set_primary_sensor":"Выбрать датчик CO₂",
    "set_data_trust":"Доверие к источнику", "set_ventilation_auto":"Автовентиляция", "set_diesel_autostart":"Автозапуск дизеля",
    "post_message":"Отправить сообщение", "read_messages":"Прочитать чат",
}


def field(name, label, kind="text", **options):
    return dict(name=name, label=label, type=kind, **options)


def num(name, label, minimum=None, maximum=None, **options):
    return field(name, label, "number", min=minimum, max=maximum, **options)


def choice(name, label, values, **options):
    return field(name, label, "select", choices=list(values), **options)


def schema(node_id, action, kind="control"):
    """Only the live catalog decides which of these forms are available."""
    fields, mode, hint = [], "none", ""
    boolean = lambda name, label: choice(name, label, [True, False])
    if kind == "query":
        mode = "object"
        if action == "list_files" and node_id == "cnc_01":
            fields = [boolean("validate", "Проверить G-code")]
        elif action == "read_messages":
            fields = [num("since_id", "После ID", 0, integer=True, optional=True), num("limit", "Количество", 1, 200, integer=True, default=50)]
        elif action != "list_files":
            fields, mode = [field("value", "Параметры JSON", "json", optional=True)], "json"
    elif action in {"line_on", "line_off", "reset_protection", "reset_line_error"}:
        mode, fields = "scalar", [num("value", "Линия щитка", 1, 8, integer=True)]
    elif action in {"enable_mppt", "disable_mppt"} and node_id == "solar_panels_01":
        mode, fields = "scalar", [num("value", "Канал MPPT", 1, integer=True)]
    elif action == "set_speed":
        low, high = (50, 200) if node_id == "printer_3d_01" else (10, 100) if node_id == "supply_ventilation_01" else (0, 100)
        mode, fields = "scalar", [num("value", "Скорость, %", low, high)]
    elif action in {"set_power_limit", "limit_load", "set_poll_interval", "set_relay_fan_level", "set_frequency", "set_threshold", "transfer_fuel"}:
        bounds = {"set_power_limit":(20,100), "limit_load":(10,100), "set_poll_interval":(50,5000),
                  "set_relay_fan_level":(0,10), "set_frequency":(430,440), "set_threshold":(.01,1000), "transfer_fuel":(.01,None)}
        labels = {"set_poll_interval":"Период, мс", "set_frequency":"Частота, МГц", "set_threshold":"Порог, мм/с", "transfer_fuel":"Объём, л"}
        mode, fields = "scalar", [num("value", labels.get(action,"Значение"), *bounds[action], integer=action in {"set_poll_interval","set_relay_fan_level"})]
    elif action in {"set_fan", "set_mode", "seal_zone", "manual_start_zone", "lock_door", "unlock_door", "set_zero", "switch_channel", "set_protocol", "set_traffic_priority", "restart_service", "set_primary_sensor"}:
        enums = {"set_fan":["AUTO","ON","OFF"], "set_mode":["RECIRCULATION","FRESH_AIR","MIXED"],
                 "seal_zone":["MAIN","FABLAB","LIVING","STORAGE"], "manual_start_zone":["FABLAB","LIVING","STORAGE","POWER_ROOM"],
                 "lock_door":["MAIN_AIRLOCK","FABLAB","STORAGE","SERVER_ROOM"], "unlock_door":["MAIN_AIRLOCK","FABLAB","STORAGE","SERVER_ROOM"],
                 "set_zero":["xyz","xy","z"], "switch_channel":["SATELLITE","LORA","MESH"],
                 "set_protocol":["AX25","FSK","LORA","DMR"] if node_id == "radio_01" else ["IRIDIUM_SBD","IP","LORAWAN","MESHTASTIC","BATMAN","OLSR"],
                 "set_traffic_priority":["NORMAL","TELEMETRY","EMERGENCY"],
                 "restart_service":["agent_runtime","telemetry_collector","vision_inference","api_gateway"],
                 "set_primary_sensor":["climate_sensor_01","climate_sensor_02","climate_sensor_03"]}
        mode, fields = "scalar", [choice("value", "Значение", enums[action])]
    elif action in {"lockdown", "set_diesel_autostart", "set_ventilation_auto"} or (action == "set_lock" and node_id == "thermal_insulation_02"):
        mode, fields = "scalar", [boolean("value", "Включено / заперто")]
    elif action == "set_lock":
        mode, fields = "object", [num("lock", "Замок", 0, 1, integer=True), boolean("locked", "Запереть")]
    elif action == "set_fan_pwm":
        mode, fields = "object", [num("fan", "Вентилятор", 0, 5 if node_id == "thermal_insulation_01" else 1, integer=True), num("pwm", "PWM", 0, 255, integer=True)]
        hint = "При PWM ниже примерно 30 вентилятор может не запуститься."
    elif action == "set_vacuum":
        mode, fields = "scalar", [choice("value", "Пылеудаление", [0, 1])]
    elif action in {"start_job", "delete_file"}:
        mode, fields = "scalar", [field("value", "Файл", "file_name")]
        hint = "Файлы берутся из телеметрии или запроса «Получить список файлов»."
    elif action == "upload_file":
        mode, fields = "object", [field("file_name", "Имя файла"), field("content", "G-code", "file_text")]
        hint = "Загружайте только G-code, подготовленный организаторами (до 16 МиБ)."
    elif action == "move":
        mode, fields = "object", [choice("mode","Режим",["relative","absolute"]),
            *[num(axis, axis.upper()+", мм", optional=True) for axis in ("x","y","z")], num("feedrate","Подача, мм/мин",1,5000,optional=True)]
    elif action == "probe_z":
        mode, fields = "object", [num("distance","Ход вниз, мм",.1,30,default=10), num("feed","Подача, мм/мин",1,300,default=50),
             num("plate_thickness","Толщина пластины, мм",0,30,default=0), num("retract","Подъём, мм",0,20,default=2),
             num("second_pass_feed","Повторная подача",1,100,optional=True), num("latch_distance","Отъезд перед повтором, мм",.1,5,optional=True)]
    elif action == "set_thresholds":
        mode, fields = "object", [num("warning","Предупреждение",0,optional=True), num("alarm","Тревога",0,optional=True)]
    elif action in {"reset_interlock", "unblock_interlock", "acknowledge"}:
        mode, fields = "scalar", [field("value", "ID события или all" if action == "acknowledge" else "ID интерлока (IL_…)")]
    elif action == "block_interlock":
        mode, fields = "object", [field("id","ID интерлока"),num("minutes","Минуты",1,30,default=10)]
    elif action == "set_data_trust":
        mode, fields = "object", [field("node","Источник (node_id)"),field("param","Параметр"),
            choice("trust","Доверие",["TRUSTED","UNRELIABLE","FAILED"]),num("line","Линия щитка",1,8,integer=True,optional=True)]
    elif action == "post_message":
        mode, fields = "object", [field("text","Сообщение","textarea",max_length=4000),field("to","Кому",default="operator"),field("name","Имя отправителя",default="web-panel")]
    elif action not in LABELS:
        mode, fields = "json", [field("value","Значение JSON (если требуется)","json",optional=True)]
    return dict(action=action, type=kind, label=LABELS.get(action,action), mode=mode, fields=fields, hint=hint)


def catalog_specs(catalog):
    result = {}
    for node in catalog:
        node_id = node.get("node_id")
        if not node_id:
            continue
        specs = [schema(node_id,a) for a in dict.fromkeys(node.get("controls",[])+node.get("service_actions",[]))]
        specs += [schema(node_id,a,"query") for a in node.get("queries",[])]
        result[node_id] = specs
    return result


def validate_value(spec, supplied):
    mode = spec["mode"]
    if mode == "none":
        if supplied is not None:
            raise ValueError("Эта команда не принимает параметров.")
        return None
    if mode == "json":
        if spec["type"] == "query" and supplied is not None and not isinstance(supplied,dict):
            raise ValueError("Параметры запроса должны быть объектом JSON.")
        return supplied
    values = {"value":supplied} if mode == "scalar" else supplied
    if not isinstance(values, dict):
        raise ValueError("Ожидается объект параметров.")
    allowed = {f["name"] for f in spec["fields"]}
    if set(values)-allowed:
        raise ValueError("Неизвестные параметры: " + ", ".join(set(values)-allowed))
    output = {}
    for f in spec["fields"]:
        value = values.get(f["name"])
        if value is None:
            if f.get("optional"):
                continue
            raise ValueError(f"Заполните поле «{f['label']}».")
        if f["type"] == "number":
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise ValueError(f"«{f['label']}»: нужно конечное число.")
            if f.get("integer") and value != int(value):
                raise ValueError(f"«{f['label']}»: нужно целое число.")
            if (f.get("min") is not None and value < f["min"]) or (f.get("max") is not None and value > f["max"]):
                raise ValueError(f"«{f['label']}»: значение вне допустимого диапазона.")
        elif f["type"] == "select":
            if not any(type(value) is type(choice) and value == choice for choice in f["choices"]):
                raise ValueError(f"«{f['label']}»: выберите допустимое значение.")
        elif not isinstance(value,str) or not value.strip():
            raise ValueError(f"«{f['label']}»: нужна непустая строка.")
        if isinstance(value,str) and len(value.encode("utf-8")) > (16*1024*1024 if f["type"]=="file_text" else f.get("max_length",4096)*4):
            raise ValueError(f"«{f['label']}»: превышен размер.")
        output[f["name"]] = int(value) if f.get("integer") else value
    if mode == "object" and spec["fields"] and not output:
        raise ValueError("Укажите хотя бы один параметр.")
    if spec["action"] == "move" and not any(axis in output for axis in ("x","y","z")):
        raise ValueError("Укажите хотя бы одну ось.")
    return output.get("value") if mode == "scalar" else output


def preconditions(node_id, action, value, nodes):
    node = nodes.get(node_id) or {}
    if action == "reconnect":
        return
    if node.get("online") is False or node.get("status") == "UNKNOWN":
        raise ValueError("Узел не на связи или его показания устарели. Сначала восстановите связь.")
    if node.get("lockout_remaining_s",0) or node.get("hold_level") == "ALARM":
        raise ValueError("Узел заблокирован автоматикой.")
    if node_id in {"printer_3d_01","cnc_01"}:
        if action == "start_job":
            if node.get("state") not in {"IDLE","COMPLETED"}:
                raise ValueError("Станок не готов к новому заданию.")
            if node_id == "printer_3d_01" and node.get("bed_clear") is not True:
                raise ValueError("Не подтверждён свободный стол принтера.")
            files = node.get("files") or []
            names = [item.get("name",item.get("file_name")) if isinstance(item,dict) else item for item in files]
            if value not in names:
                raise ValueError("Выберите файл из списка файлов станка.")
        if action == "resume":
            reason = node.get("hold_reason")
            interlock = (nodes.get("dome_automation_01") or {}).get("interlocks",{}).get(reason,{})
            if interlock.get("state") == "TRIPPED":
                raise ValueError("Причина удержания не устранена: интерлок ещё TRIPPED.")
        if action in {"home","move","set_zero","probe_z"} and node.get("state") in {"PRINTING","RUNNING","PAUSED","BUSY","HOMING"}:
            raise ValueError("Дождитесь остановки задания перед наладкой станка.")
        if action == "move":
            if node.get("homed") is not True:
                raise ValueError("Сначала выполните HOME.")
            if value.get("mode") == "relative":
                limits = {"x":50,"y":50,"z":50} if node_id == "printer_3d_01" else {"x":300,"y":180,"z":80}
                if any(abs(value.get(axis,0)) > limit for axis,limit in limits.items()):
                    raise ValueError("Перемещение превышает допустимый ход оси.")


def expectation(node_id, action, value, nodes):
    node = nodes.get(node_id) or {}
    def expected(path, target):
        return dict(path=path, target=target)
    if action in {"line_on","line_off","reset_protection"}:
        return expected(["lines",value-1,"state"],"OFF" if action=="line_off" else "ON")
    scalar = {"set_speed":"speed_pct","set_fan":"fan_mode","set_mode":"mode","set_frequency":"frequency_mhz",
              "set_protocol":"protocol","switch_channel":"active_channel","set_traffic_priority":"traffic_priority",
              "limit_load":"load_limit_pct","set_power_limit":"power_limit_pct","set_poll_interval":"poll_interval_ms",
              "set_relay_fan_level":"relay_fan_level","set_primary_sensor":"primary_co2_sensor",
              "set_diesel_autostart":"diesel_autostart","set_ventilation_auto":"ventilation_auto"}
    if action in scalar:
        return expected([scalar[action]],value)
    if action == "set_fan_pwm":
        return expected(["fans_pwm",value["fan"]],value["pwm"])
    if action == "set_lock":
        if node_id == "thermal_insulation_01":
            return expected(["locks",value["lock"]],"LOCKED" if value["locked"] else "UNLOCKED")
        return expected(["lock"],value if isinstance(node.get("lock"),bool) else "LOCKED" if value else "UNLOCKED")
    if action == "home":
        return expected(["homed"],True)
    if action in {"resume","start_job"}:
        return expected(["state"],"PRINTING" if node_id == "printer_3d_01" else "RUNNING")
    if action in {"lock_door","unlock_door"}:
        return expected(["doors",value,"locked"],action=="lock_door")
    if action == "lockdown":
        return expected(["lockdown"],value)
    if action in {"enable_mppt","disable_mppt"} and node_id == "solar_inverter_01":
        return expected(["mppt_enabled"],action=="enable_mppt")
    if action in {"set_mode_hybrid","set_mode_grid_only"}:
        return expected(["mode"],"HYBRID" if action=="set_mode_hybrid" else "GRID_ONLY")
    if action in {"turn_on","turn_off"} and "enabled" in node:
        return expected(["enabled"],action=="turn_on")
    if action == "reconnect":
        return expected(["status"],"OK")
    return None


def read_path(node, path):
    for key in path:
        try:
            node = node[key]
        except (KeyError,IndexError,TypeError):
            return None
    return node
