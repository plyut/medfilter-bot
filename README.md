# МедФильтр

Telegram-бот для проверки медицинской информации из соцсетей, видео и мессенджеров.
Проект для кейс-чемпионата «Медицина 2050», трек «Школьники».

Пользователь пересылает пост, скриншот, фото или видео. Бот:

1. **Извлекает содержание**: текст сообщения, текст и смысл изображений, ключевые кадры и речь из видео.
2. **Отсеивает ненужное**: рекламу, эмоции, «воду».
3. **Выделяет утверждения** с учётом оговорок автора («возможно», «в некоторых случаях»).
4. **Проверяет каждое** по научному консенсусу: клинические рекомендации Минздрава РФ, ВОЗ, систематические обзоры. Ставит вердикт и уровень достоверности доказательств (УДД 1–5).
5. **Объясняет**: краткая сводка, балл достоверности 0–100, признаки манипуляции, ссылки на источники, советы, как проверить самому.

## Как устроено

```
Telegram ─► bot.py ─► collect(): текст, фото, видео
                         │  видео → ffmpeg: 4 кадра + звук → Yandex SpeechKit → текст
                         ▼
                   checker.py: скрытие персональных данных → кэш → очередь
                         ▼
                   llm.py: GigaChat (текст + изображения) → JSON
                         ▼
                   render.py: короткий ответ + кнопки «Подробнее», «Источники», «Как проверить самому»
```

| Файл | Что делает |
|---|---|
| `bot.py` | Обработчики Telegram: команды, сообщения, альбомы, кнопки |
| `medfilter/prompt.py` | Инструкция для нейросети и разбор JSON-ответа |
| `medfilter/llm.py` | Запросы к GigaChat с изображениями |
| `medfilter/media.py` | Кадры и звук из видео через ffmpeg |
| `medfilter/speech.py` | Распознавание речи Yandex SpeechKit |
| `medfilter/checker.py` | Конвейер: кэш, очередь, повтор при ошибке формата |
| `medfilter/cache.py` | Кэш результатов в SQLite с перепроверкой через 30 дней |
| `medfilter/privacy.py` | Скрытие телефонов, e-mail, СНИЛС, паспортов и полисов |
| `medfilter/sources.py` | Каталог источников: ссылки собирает код, а не нейросеть |
| `medfilter/render.py` | Оформление ответов |

### Решения из кейса

- **Достоверность ссылок.** Нейросеть может выдумать URL, поэтому она выбирает только источник из списка (ВОЗ, Минздрав, PubMed, Cochrane и др.) и поисковый запрос. Ссылку собирает код.
- **Нагрузка.** Одновременно к нейросети уходит не больше `MAX_PARALLEL_CHECKS` запросов, остальные ждут в очереди. Одинаковые материалы (популярный фейк, который пересылают тысячи раз) берутся из кэша.
- **Актуальность.** Сохранённый результат перепроверяется через `CACHE_DAYS` дней (по умолчанию 30).
- **Персональные данные (152-ФЗ).** Бот не хранит имена, контакты и переписку. Телефоны, e-mail, СНИЛС, номера паспортов и полисов скрываются до отправки в нейросеть. Файлы удаляются сразу после обработки. Нейросеть и распознавание речи — российские сервисы.
- **Обучение пользователя.** К каждому ответу прилагается подсказка, как проверить самому, и памятка «6 шагов проверки».

## Запуск

Нужны Python 3.11+ и ffmpeg.

1. Создайте бота у [@BotFather](https://t.me/BotFather) (`/newbot`) и скопируйте токен.
2. Получите ключ GigaChat API: [developers.sber.ru](https://developers.sber.ru/studio) → GigaChat API → «Получить ключ» → Authorization Key. Для физлиц есть бесплатный лимит.
3. (Необязательно, для речи из видео) В [Yandex Cloud](https://console.yandex.cloud) создайте сервисный аккаунт с ролью `ai.speechkit-stt.user`, выпустите API-ключ и скопируйте ID каталога.
4. Установите и запустите:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; на Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # на Linux/macOS: cp .env.example .env
# заполните .env
python bot.py
```

### Запуск на сервере (systemd)

Так бот работает сейчас: виртуальная машина Yandex Cloud, Ubuntu 22.04, 2 vCPU 20 %, 1 ГБ.

```bash
sudo apt install -y python3-venv ffmpeg
sudo mkdir -p /opt/medfilter && sudo chown $USER /opt/medfilter
# скопируйте проект и .env в /opt/medfilter
cd /opt/medfilter && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Файл `/etc/systemd/system/medfilter.service`:

```ini
[Unit]
Description=MedFilter Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/medfilter
ExecStart=/opt/medfilter/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now medfilter
journalctl -u medfilter -f     # журнал
```

### Запуск на сервере (Docker)

```bash
docker build -t medfilter .
docker run -d --restart unless-stopped --env-file .env -v $(pwd)/data:/app/data medfilter
```

## Тесты

```bash
python -m unittest discover tests
```

## Ограничения

- Бот помогает разобраться в информации, но не ставит диагнозов и не заменяет врача.
- Вывод формирует нейросеть, она может ошибаться, поэтому ответ всегда сопровождается источниками.
- Telegram отдаёт ботам файлы до 20 МБ.
