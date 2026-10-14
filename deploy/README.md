# Развёртывание на сервере

Бот, дашборд и Caddy (HTTPS) работают в Docker Compose на одном VPS.

```
MAX ──https──▶ Caddy :443 ──/max-webhook──▶ bot :8080
браузер ─https─▶ Caddy :443 ──всё остальное──▶ web :8000
                         bot и web — общая база data/bot.db
```

## Что нужно

- VPS **в России** (152-ФЗ: персональные данные сотрудников хранятся в РФ), Ubuntu 22.04 или 24.04, 1 ядро, 1–2 ГБ памяти, доступ по SSH.
- Домен или поддомен, например `dispatch.example.ru`.
- Токен бота из @Masterbot.

## 0. Перед началом

1. **Перевыпустите токен бота** в @Masterbot. Старый токен был в переписке, его лучше не использовать на сервере.
2. **Остановите бота на своём компьютере.** Иначе после включения webhook он всё равно перестанет получать события. Для разработки дальше заведите отдельного тестового бота со своим токеном: если на основном боте есть webhook, локальный polling сам откажется запускаться.

## 1. DNS

В панели домена создайте **A-запись** `dispatch` → IP сервера. Проверка с любого компьютера:

```bash
dig +short dispatch.example.ru
```

Должен вернуться IP сервера. Пока его нет, Caddy не сможет получить сертификат.

## 2. Сервер

Вход только по SSH-ключу (пароли выключены), работаем не от root. Затем:

```bash
sudo mkdir -p /opt/dispatch && sudo chown "$USER" /opt/dispatch
git clone https://github.com/ModernMan-n/DispetcherBotMax.git /opt/dispatch
cd /opt/dispatch
bash deploy/setup-server.sh
```

Скрипт ставит Docker, git и sqlite3, открывает в firewall только порты 22, 80 и 443 и готовит папку `data`. После него **перелогиньтесь**.

**Если `docker pull` не работает** (Docker Hub ограничил доступ из России), подключите зеркало в `/etc/docker/daemon.json`:

```json
{ "registry-mirrors": ["https://mirror.gcr.io"] }
```

У многих российских хостингов есть своё зеркало, оно указано в их документации. После правки выполните `sudo systemctl restart docker`.

## 3. Настройки

```bash
cd /opt/dispatch
cp .env.example .env
chmod 600 .env
nano .env
```

| Переменная | Значение |
|---|---|
| `MAX_BOT_TOKEN` | новый токен из @Masterbot |
| `DOMAIN` | `dispatch.example.ru` |
| `WEBHOOK_URL` | `https://dispatch.example.ru/max-webhook` |
| `WEBHOOK_SECRET` | случайная строка: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DISPATCHER_CHAT_ID` | ID чата диспетчерской (бот присылает его в личку, когда его добавляют в чат) |
| `STAFF_CHAT_ID` | ID чата «Сотрудники»; пусто — проверка выключена |
| `DATABASE_URL` | оставить как есть |

`BOT_MODE` в `.env` можно не трогать: на сервере `docker-compose.yml` сам ставит `webhook`.

## 4. Данные

**Вариант А — начать с чистой базы.** Ничего делать не нужно, база создастся при запуске.

**Вариант Б — перенести текущую базу с компьютера** (заявки, специалисты, роли). На компьютере, при остановленном боте:

```bash
sqlite3 data/bot.db "PRAGMA wal_checkpoint(TRUNCATE);"
scp data/bot.db сервер:/opt/dispatch/data/bot.db
```

На сервере выполните `sudo chown 1000:1000 /opt/dispatch/data/bot.db`.

## 5. Запуск

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

В логе бота должны быть строки `Webhook: https://…/max-webhook` и `Зарегистрировано … обработчиков`. Откройте `https://dispatch.example.ru`: должна появиться страница входа с действующим сертификатом.

## 6. Первый администратор (для варианта А)

```bash
docker compose exec bot python -m scripts.staff add "Ваше имя" +79001234567 network computers --role admin
```

Затем напишите боту `/staff`, нажмите «Поделиться номером» и войдите в дашборд по коду. Остальных сотрудников добавляйте уже в разделе «Сотрудники».

## Ежедневная работа

| Задача | Команда (в `/opt/dispatch`) |
|---|---|
| Обновить до новой версии | `git pull && docker compose up -d --build` |
| Логи | `docker compose logs -f bot` / `web` / `caddy` |
| Перезапуск | `docker compose restart bot web` |
| Остановить всё | `docker compose down` |
| Сотрудники из консоли | `docker compose exec bot python -m scripts.staff list` |
| Резервная копия вручную | `docker compose exec -T bot python -m scripts.backup` |

Миграции базы применяются сами при каждом запуске (сервис `migrate`).

## Резервные копии

Ежедневная копия в 03:00 (хранятся 30 последних, папка `data/backup/`). Добавить через `crontab -e`:

```
0 3 * * * cd /opt/dispatch && docker compose exec -T bot python -m scripts.backup >> $HOME/dispatch-backup.log 2>&1
```

Копии на том же сервере не спасут, если пропадёт сам сервер. Раз в неделю забирайте их к себе или в хранилище хостинга:

```bash
scp -r сервер:/opt/dispatch/data/backup ./dispatch-backup
```

Восстановление: остановить (`docker compose down`), положить копию на место `data/bot.db`, запустить.

## Если что-то не работает

- **Нет сертификата / сайт не открывается.** Проверьте DNS (шаг 1) и что порты 80 и 443 открыты (`sudo ufw status`). Смотрите `docker compose logs caddy`.
- **Бот молчит.** Смотрите `docker compose logs bot`. Проверьте, что `WEBHOOK_URL` совпадает с доменом и что бот на компьютере остановлен.
- **Код входа не приходит.** Сотрудник должен хотя бы раз написать боту `/staff`. Если задан `STAFF_CHAT_ID`, он ещё должен состоять в чате «Сотрудники», а бот — быть в этом чате администратором.
