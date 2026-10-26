# Dome_RAG

Python-клиент и локальная веб-панель для API купола. Панель показывает телеметрию, каталог устройств и позволяет вручную отправлять разрешённые команды с проверкой результата.

## Запуск на Windows

Требуется Python 3.11 или новее.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

В `.env` задайте `DOME_URL` и свой `DOME_KEY`, затем запустите:

```powershell
.\.venv\Scripts\python.exe dome_web.py
```

Откройте [локальную панель](http://127.0.0.1:8780/). Адрес и ключ хранятся на Python-сервере; ключ не передаётся в браузер и исключён из Git.

Консольная проверка соединения:

```powershell
.\.venv\Scripts\python.exe dome_client.py --once
```

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Подробности подключения, параметров, управления и журналов — в [CLIENT.md](CLIENT.md). Документация API и база знаний находятся в корне проекта и `knowledge/`.
