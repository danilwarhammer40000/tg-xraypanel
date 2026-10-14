# Хост подписок (отдельный сервер)

Если порт 443 основного сервера занят, подписки отдаёт отдельный хост. Бот пишет файлы подписок у себя и доставляет их на хост через rsync
(`SUB_PUSH`). Хост ничего не знает о боте: это nginx, сертификат и папка.

Хостом может быть любой VPS (в том числе тот, где уже стоит nginx для старого
стенда). Нужны: домен с A-записью на хост, открытые порты 80 и 443, `rsync`.

## 1. На хосте подписок

```bash
apt update && apt install -y nginx certbot rsync jq
mkdir -p /var/www/sub/s /var/www/sub/routing /var/www/acme
adduser --disabled-password --gecos "" sub
chown -R sub:sub /var/www/sub/s
```

Сертификат (замените домен и почту):

```bash
cat > /etc/nginx/conf.d/sub-http.conf <<'EOT'
server {
    listen 80;
    server_name config.example.net;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 404; }
}
EOT
nginx -t && systemctl reload nginx
certbot certonly --webroot -w /var/www/acme -d config.example.net \
  --deploy-hook "systemctl reload nginx" -m you@example.net --agree-tos -n
```

Основной конфиг: скопируйте `sub-https.conf.example` в
`/etc/nginx/conf.d/sub-https.conf`, подставьте домен и заголовок профиля,
затем `nginx -t && systemctl reload nginx`.

Страница с кнопками (разово, из этой папки репозитория):

```bash
cp join.html /var/www/sub/routing/join.html
```

**Маршрутизация приезжает вместе с подпиской.** Генератор пишет профили в
`/var/www/sub/routing/` и кусок конфигурации nginx с двумя заголовками, которые
добавляются к каждому ответу подписки: `autorouting` (его читает Incy: профиль
скачивается и обновляется сам) и `routing` (его читает Happ: профиль внутри заголовка).
Приложение применяет профиль и скачивает гео-файлы при импорте и при каждом
обновлении подписки, отдельная кнопка людям не нужна:

```bash
python3 gen_routing.py --domain config.example.net --reload     # режим ru: российские сайты напрямую
# python3 gen_routing.py --domain config.example.net --mode wl --reload   # режим «белый список»
```

В `sub-https.conf` (пример в `sub-https.conf.example`) внутри `location` подписки должна
быть строка `include /etc/nginx/snippets/sub-routing-*.conf;`: если конфиг у вас старый,
добавьте её перед `try_files`. Правила меняются правкой `gen_routing.py` и повторным
запуском: клиенты подтянут новый профиль при ближайшем обновлении подписки (в
Incy профиль перекачивается раз в сутки по расписанию автороутинга, гео-файлы по
своему интервалу).

Проверка заголовков (токен возьмите из `xray_cli urls` или `users.list`):

```bash
curl -sI https://config.example.net/s/ТОКЕН | grep -iE '^(routing|autorouting|profile-title):' | cut -c1-110
```

Если на хосте раньше работал `gen-sub.py` со своим таймером, **отключите его**
(`systemctl disable --now gen-sub.timer`): он удаляет из папки подписок всё,
чего нет в его `users.list`, и стёр бы файлы, которые присылает бот.

## 2. Ключ для доставки (на сервере с ботом)

```bash
ssh-keygen -t ed25519 -N "" -f /root/.ssh/sub_push
cat /root/.ssh/sub_push.pub
```

Строку из `.pub` добавьте на **хосте** в `/home/sub/.ssh/authorized_keys`
(папка `.ssh` с правами 700, файл 600, владелец `sub`), дописав в начало
ограничения:

```
restrict,from="IP_СЕРВЕРА_С_БОТОМ" ssh-ed25519 AAAA... root@server
```

На сервере с ботом привяжите ключ к хосту:

```bash
cat >> /root/.ssh/config <<'EOT'
Host IP_ХОСТА_ПОДПИСОК
    IdentityFile /root/.ssh/sub_push
    IdentitiesOnly yes
EOT
chmod 600 /root/.ssh/config
apt install -y rsync dnsutils
ssh -i /root/.ssh/sub_push sub@IP_ХОСТА_ПОДПИСОК true     # принять отпечаток, команда должна завершиться без ошибки
```

Юнит бота запускается с `ProtectHome=read-only`: ключ в `/root/.ssh` он читает,
но писать в `/root` не может, поэтому принять отпечаток хоста (`ssh ... true` выше)
нужно один раз вручную до запуска. Если хост подписок на этом же сервере,
`push` не нужен: укажите папку в `"dir"`, а `/var/www/sub` уже разрешена для записи
юнитом (другой путь добавьте через `systemctl edit xraypanel-bot.service`,
`ReadWritePaths=`).

Ограничение `restrict` в `authorized_keys` разрешает вход только с IP сервера,
но не запрещает rsync: ключ может писать только в то, что доступно
пользователю `sub` (то есть в `/var/www/sub/s`).

## 3. Настройка бота

В `data/xray.json` на сервере с ботом добавьте секцию `subscription`
(см. пример в `xray.json.example` рядом); она входит в бэкап бота:

```bash
cd /opt/xraypanel
venv/bin/python - <<'PY'
import json
p = "data/xray.json"
d = json.load(open(p))
d["subscription"] = {
    "domain": "config.example.net",
    "push": ["sub@IP_ХОСТА_ПОДПИСОК:/var/www/sub/s/"],
    "endpoints": [
        {"label": "yandex",   "host": "cdn.example.net", "mode": "resolve"},
        {"label": "yandex-d", "host": "cdn.example.net", "mode": "domain"},
    ],
}
json.dump(d, open(p, "w"), indent=2)
PY
chmod 600 data/xray.json
venv/bin/python -m core.xray_cli publish
venv/bin/python -m core.xray_cli urls
```

Проверка с любого устройства:

```bash
curl -s https://config.example.net/s/ТОКЕН | base64 -d | head -2     # должны быть vless://
curl -sI https://config.example.net/s/ТОКЕН | grep -i profile
```

## 4. Перенос людей из старого `users.list`

Подробная инструкция с выгрузкой базы и восстановлением через бота: `ADOPT.md`
в корне репозитория. Коротко, на сервере с ботом:

```bash
venv/bin/python -m core.xray_cli adopt            # план, ничего не пишет
venv/bin/python -m core.xray_cli adopt --apply    # записать xray.json
venv/bin/python -m core.xray_cli publish
```

UUID и токены сохраняются, поэтому выданные ссылки продолжают работать.
Перенесённые люди помечаются как «внешние»: срок и статус из `users.json` к ним
не применяются. После переноса **не запускайте** `xray-users.sh`: он
переписывает список клиентов целиком и сотрёт клиентов панели (`tp:`).
