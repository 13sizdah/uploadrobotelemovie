# راهنمای کامل دستورات سرور ربات تبدیل فایل به لینک

این راهنما برای Ubuntu 22.04/24.04 و Debian 12 نوشته شده است. دستورها را فقط روی سروری اجرا کنید که برای این ربات در نظر گرفته‌اید.

## مقادیر قابل جایگزینی

در دستورهای این فایل، موارد زیر را با اطلاعات واقعی خود عوض کنید:

| مقدار نمونه | جایگزین با |
|---|---|
| `SERVER_IP` | IP سرور فعلی |
| `NEW_SERVER_IP` | IP سرور جدید |
| `YOUR_IP` | IP اینترنت کامپیوتر مدیر |
| `download.example.com` | دامنه دانلود |
| `123456789` | شناسه عددی مدیر تلگرام |
| `TIMESTAMP` | تاریخ موجود در نام فایل بکاپ |

## ۱. اتصال به سرور

اتصال با کاربر root:

```bash
ssh root@SERVER_IP
```

اتصال با کاربر عادی:

```bash
ssh USERNAME@SERVER_IP
```

تمام دستورهای مدیریتی را با کاربر دارای دسترسی `sudo` اجرا کنید.

## ۲. بررسی سیستم‌عامل و منابع

نمایش نسخه سیستم‌عامل:

```bash
cat /etc/os-release
```

نمایش RAM و Swap:

```bash
free -h
```

نمایش فضای دیسک:

```bash
df -h
```

نمایش حجم پوشه‌های پروژه:

```bash
sudo du -h --max-depth=1 /opt/uploadrobotelemovie
```

نمایش IP عمومی سرور:

```bash
curl -4 https://icanhazip.com
```

## ۳. دریافت پروژه از GitHub

نصب Git:

```bash
sudo apt update
sudo apt install -y git
```

دریافت مخزن عمومی:

```bash
cd /opt
sudo git clone https://github.com/13sizdah/uploadrobotelemovie.git
sudo chown -R "$USER":"$USER" /opt/uploadrobotelemovie
cd /opt/uploadrobotelemovie
```

اگر مخزن خصوصی و SSH Key روی GitHub ثبت شده است:

```bash
cd /opt
sudo git clone git@github.com:13sizdah/uploadrobotelemovie.git
sudo chown -R "$USER":"$USER" /opt/uploadrobotelemovie
cd /opt/uploadrobotelemovie
```

نمایش نسخه فعلی سورس:

```bash
git log -1 --oneline
```

## ۴. نصب خودکار

اجرای نصب‌کننده:

```bash
cd /opt/uploadrobotelemovie
sudo bash install.sh
```

این دستور Docker، Docker Compose، Nginx و Certbot را نصب و صفحه نصب را روی پورت `9090` اجرا می‌کند. لینک امن صفحه نصب در ترمینال نمایش داده می‌شود.

بازکردن موقت صفحه نصب فقط برای IP مدیر:

```bash
sudo ufw allow from YOUR_IP to any port 9090 proto tcp
```

مشاهده وضعیت فایروال:

```bash
sudo ufw status numbered
```

بعد از اتمام نصب، قانون پورت 9090 را حذف کنید:

```bash
sudo ufw delete allow from YOUR_IP to any port 9090 proto tcp
```

اگر UFW هنوز فعال نیست، پیش از `ufw enable` حتماً SSH را مجاز کنید تا دسترسی خود را از دست ندهید:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

## ۵. مدیریت تنظیمات

ورود به پوشه پروژه:

```bash
cd /opt/uploadrobotelemovie
```

ویرایش تنظیمات:

```bash
sudo nano .env
```

مهم‌ترین تنظیمات:

```env
BOT_TOKEN="TOKEN_FROM_BOTFATHER"
PUBLIC_BASE_URL="https://download.example.com"
FILE_TTL_HOURS=24
MAX_FILE_SIZE_MB=0
ADMIN_USER_ID=123456789
FORWARD_ONLY=true
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH="0123456789abcdef0123456789abcdef"
TELEGRAM_API_BASE=http://telegram-bot-api:8081
```

کاربرد متغیرها:

- `BOT_TOKEN`: توکن دریافتی از BotFather.
- `PUBLIC_BASE_URL`: دامنه عمومی صفحه دانلود بدون `/` پایانی.
- `FILE_TTL_HOURS`: تعداد ساعت نگهداری فایل.
- `MAX_FILE_SIZE_MB=0`: بدون محدودیت حجم داخلی.
- `ADMIN_USER_ID`: شناسه عددی تنها مدیر پنل `/admin`.
- `FORWARD_ONLY`: وضعیت اولیه محدودیت فایل‌های فورواردی در اولین اجرا.
- `TELEGRAM_API_ID` و `TELEGRAM_API_HASH`: اطلاعات دریافتی از `my.telegram.org/apps`.
- `TELEGRAM_API_BASE`: آدرس Local Telegram Bot API داخل Docker.

محافظت از فایل تنظیمات:

```bash
sudo chmod 600 .env
```

پس از تغییر `.env` سرویس را بازسازی کنید:

```bash
docker compose up -d --build
```

## ۶. دستورات روزانه Docker

نمایش وضعیت سرویس‌ها:

```bash
cd /opt/uploadrobotelemovie
docker compose ps
```

اجرای سرویس‌ها:

```bash
docker compose up -d
```

بازسازی و اجرای نسخه جدید:

```bash
docker compose up -d --build
```

توقف موقت سرویس‌ها:

```bash
docker compose stop
```

اجرای سرویس‌های متوقف‌شده:

```bash
docker compose start
```

راه‌اندازی مجدد:

```bash
docker compose restart
```

خاموش‌کردن و حذف کانتینرها بدون حذف فایل‌های `data`:

```bash
docker compose down
```

هشدار: دستور زیر volume مربوط به Local Telegram API را هم حذف می‌کند؛ برای استفاده روزمره اجرا نکنید:

```bash
docker compose down --volumes
```

## ۷. مشاهده گزارش‌ها

نمایش زنده همه گزارش‌ها:

```bash
docker compose logs -f
```

فقط گزارش ربات:

```bash
docker compose logs -f file-link-bot
```

فقط گزارش Local Telegram Bot API:

```bash
docker compose logs -f telegram-bot-api
```

نمایش ۲۰۰ خط آخر بدون دنبال‌کردن:

```bash
docker compose logs --tail=200 file-link-bot
```

خروج از حالت نمایش زنده گزارش با `Ctrl+C` انجام می‌شود و ربات را متوقف نمی‌کند.

## ۸. بررسی سلامت سرویس

بررسی برنامه از داخل سرور:

```bash
curl -i http://127.0.0.1:8080/health
```

بررسی از دامنه عمومی:

```bash
curl -i https://download.example.com/health
```

پاسخ سالم باید شامل موارد زیر باشد:

```text
HTTP/1.1 200 OK
{"status":"ok"}
```

نمایش پورت‌های در حال استفاده:

```bash
sudo ss -lntp
```

بررسی وضعیت Docker در سیستم:

```bash
sudo systemctl status docker
```

فعال‌کردن اجرای خودکار Docker بعد از reboot:

```bash
sudo systemctl enable --now docker
```

## ۹. پنل مدیریت تلگرام

مدیر داخل ربات این دستور را ارسال می‌کند:

```text
/admin
```

از پنل می‌توان:

- محدودیت پذیرش فایل‌های فورواردی را روشن یا خاموش کرد.
- کانال یا ربات مبدأ مجاز اضافه کرد.
- منابع ثبت‌شده را مشاهده کرد.
- یک منبع را حذف کرد.
- وضعیت فضای دیسک و زمان فعالیت ربات را مشاهده کرد.
- آمار فایل‌ها و حجم مصرفی را مشاهده کرد.
- فایل‌های فعال اخیر را با تأیید حذف کرد.
- پاک‌سازی فایل‌های منقضی را فوراً اجرا کرد.
- بکاپ سازگار دیتابیس را در تلگرام دریافت کرد.

برای افزودن مبدأ، ابتدا «افزودن مبدأ مجاز» را بزنید و سپس یک فایل را مستقیماً از کانال یا ربات موردنظر فوروارد کنید.

فرمان‌های متنی معادل برای مدیر:

```text
/status   وضعیت ربات و فضای دیسک
/stats    آمار فایل‌ها و منابع
/cleanup  حذف فوری فایل‌های منقضی
/backup   دریافت بکاپ متادیتای SQLite
```

فرمان `/backup` فایل‌های اصلی و `.env` را ارسال نمی‌کند. برای مهاجرت کامل از `scripts/backup.sh` استفاده کنید.

### فعال‌کردن پنل وب مدیریت در نصب موجود

```bash
cd /opt/uploadrobotelemovie
python3 scripts/hash_password.py
sudo nano .env
```

خط `ADMIN_WEB_PASSWORD_HASH` تولیدشده را در `.env` قرار دهید، سپس:

```bash
docker compose up -d --build --force-recreate file-link-bot
```

پنل از مسیر زیر در دسترس است:

```text
https://download.example.com/manage/
```

در پنل می‌توان یک یا چند S3 را با Endpoint، Bucket، Region و کلیدها افزود. اتصال پیش از ذخیره آزمایش و تنظیمات در `data/s3-backends.enc` رمزگذاری می‌شوند؛ کلید رمزگشایی در `data/config.key` است.

همچنین می‌توان تعداد نسخه‌های هر فایل را بین ۱ تا ۵ تنظیم کرد و فایل‌های فعال قدیمی را با دکمه «شروع انتقال امن به S3» منتقل کرد. تا وقتی پنل پایان عملیات را اعلام نکرده است کانتینر را متوقف یا بازسازی نکنید. در صورت شکست یک فایل، نسخه محلی آن حفظ می‌شود و می‌توان عملیات را دوباره اجرا کرد.

هر فضای S3 در پنل دارای تنظیم اولویت، دکمه فعال/غیرفعال‌کردن آپلود و حذف است. حذف تا وقتی فایل یا replica به آن فضا وابسته باشد مسدود می‌ماند. غیرفعال‌سازی فقط جلوی آپلود جدید را می‌گیرد و داده‌های قبلی را پاک نمی‌کند.

ربات پیش از هدایت کاربر به S3، وجود واقعی object را بررسی می‌کند و اگر نسخه اصلی در دسترس نباشد به replica سالم بعدی می‌رود.

## ۱۰. تنظیم و بررسی Nginx

بررسی صحت تنظیمات Nginx:

```bash
sudo nginx -t
```

بارگذاری مجدد تنظیمات بدون قطع سرویس:

```bash
sudo systemctl reload nginx
```

راه‌اندازی مجدد Nginx:

```bash
sudo systemctl restart nginx
```

نمایش وضعیت Nginx:

```bash
sudo systemctl status nginx
```

مشاهده تنظیم سایت ربات:

```bash
sudo nano /etc/nginx/sites-available/telegram-file-link-bot
```

گزارش خطای Nginx:

```bash
sudo tail -f /var/log/nginx/error.log
```

گزارش درخواست‌های Nginx:

```bash
sudo tail -f /var/log/nginx/access.log
```

## ۱۱. HTTPS و Certbot

دریافت یا نصب مجدد گواهی HTTPS:

```bash
sudo certbot --nginx -d download.example.com
```

نمایش گواهی‌های نصب‌شده:

```bash
sudo certbot certificates
```

تست تمدید خودکار بدون تغییر واقعی:

```bash
sudo certbot renew --dry-run
```

بررسی DNS دامنه:

```bash
getent hosts download.example.com
```

IP نمایش‌داده‌شده باید IP همین سرور باشد.

## ۱۲. به‌روزرسانی از GitHub

مشاهده تغییرات محلی پیش از به‌روزرسانی:

```bash
cd /opt/uploadrobotelemovie
git status
```

دریافت آخرین تغییرات:

```bash
git pull origin main
```

بازسازی سرویس پس از دریافت سورس:

```bash
docker compose up -d --build
```

بررسی نهایی:

```bash
docker compose ps
docker compose logs --tail=100 file-link-bot
curl -i https://download.example.com/health
```

## ۱۳. بکاپ کامل

ساخت بکاپ کامل شامل `.env`، دیتابیس و فایل‌ها:

```bash
cd /opt/uploadrobotelemovie
sudo ./scripts/backup.sh /var/backups/uploadrobotelemovie full
```

بکاپ سبک شامل تنظیمات و دیتابیس، بدون فایل‌های آپلودشده:

```bash
sudo ./scripts/backup.sh /var/backups/uploadrobotelemovie metadata
```

نگهداری بکاپ‌ها برای ۳۰ روز:

```bash
sudo BACKUP_RETENTION_DAYS=30 ./scripts/backup.sh /var/backups/uploadrobotelemovie full
```

نمایش بکاپ‌ها:

```bash
sudo ls -lh /var/backups/uploadrobotelemovie
```

کنترل دستی checksum:

```bash
cd /var/backups/uploadrobotelemovie
sudo sha256sum -c uploadrobotelemovie-full-TIMESTAMP.tar.sha256
```

## ۱۴. بکاپ خودکار روزانه

ویرایش cron کاربر root:

```bash
sudo crontab -e
```

برای بکاپ کامل هر روز ساعت ۳ بامداد، این خط را اضافه کنید:

```cron
0 3 * * * cd /opt/uploadrobotelemovie && BACKUP_RETENTION_DAYS=7 ./scripts/backup.sh /var/backups/uploadrobotelemovie full >> /var/log/uploadrobotelemovie-backup.log 2>&1
```

نمایش cronهای ثبت‌شده:

```bash
sudo crontab -l
```

مشاهده گزارش بکاپ خودکار:

```bash
sudo tail -f /var/log/uploadrobotelemovie-backup.log
```

نکته: بکاپ روی همان سرور در خرابی کامل دیسک کافی نیست. فایل بکاپ را به سرور یا فضای ذخیره‌سازی دیگری منتقل کنید.

## ۱۵. انتقال بکاپ به سرور جدید

انتقال با `scp`:

```bash
sudo scp /var/backups/uploadrobotelemovie/uploadrobotelemovie-full-TIMESTAMP.tar* root@NEW_SERVER_IP:/root/
```

انتقال قابل ادامه برای فایل‌های بزرگ با `rsync`:

```bash
sudo rsync -ah --progress --partial /var/backups/uploadrobotelemovie/uploadrobotelemovie-full-TIMESTAMP.tar* root@NEW_SERVER_IP:/root/
```

## ۱۶. بازیابی روی سرور جدید

ابتدا پروژه را دریافت کنید:

```bash
cd /opt
sudo git clone https://github.com/13sizdah/uploadrobotelemovie.git
sudo chown -R "$USER":"$USER" /opt/uploadrobotelemovie
cd /opt/uploadrobotelemovie
```

سپس بکاپ را بازیابی کنید:

```bash
sudo ./scripts/restore.sh /root/uploadrobotelemovie-full-TIMESTAMP.tar
```

پس از بازیابی، DNS دامنه را به IP سرور جدید تغییر دهید و سرویس را بررسی کنید:

```bash
docker compose ps
docker compose logs --tail=200 file-link-bot
curl -i http://127.0.0.1:8080/health
```

بعد از اطمینان از صحت سرور جدید، HTTPS را برای دامنه فعال یا تمدید کنید:

```bash
sudo certbot --nginx -d download.example.com
```

## ۱۷. بررسی و مدیریت فضای دیسک

فضای آزاد:

```bash
df -h
```

حجم فایل‌های ربات:

```bash
sudo du -sh /opt/uploadrobotelemovie/data
sudo du -sh /opt/uploadrobotelemovie/data/files
```

بزرگ‌ترین فایل‌ها برای بررسی، بدون حذف:

```bash
sudo find /opt/uploadrobotelemovie/data/files -type f -printf '%s %p\n' | sort -nr | head -20
```

نمایش مصرف فضای Docker:

```bash
docker system df
```

پاک‌کردن cacheهای build بدون حذف کانتینر یا volume فعال:

```bash
docker builder prune
```

این دستور تأیید می‌خواهد. از `docker system prune --volumes` استفاده نکنید، چون ممکن است داده‌های موردنیاز را حذف کند.

## ۱۸. عیب‌یابی سریع

### ربات پاسخ نمی‌دهد

```bash
cd /opt/uploadrobotelemovie
docker compose ps
docker compose logs --tail=200 file-link-bot
```

### Local Telegram API خطا دارد

```bash
docker compose logs --tail=200 telegram-bot-api
```

`TELEGRAM_API_ID` و `TELEGRAM_API_HASH` را در `.env` بررسی کنید.

### لینک دانلود باز نمی‌شود

```bash
curl -i http://127.0.0.1:8080/health
curl -i https://download.example.com/health
sudo nginx -t
sudo tail -100 /var/log/nginx/error.log
```

### خطای 502 Bad Gateway

ابتدا مطمئن شوید کانتینر ربات فعال است:

```bash
docker compose ps
docker compose restart file-link-bot
docker compose logs --tail=200 file-link-bot
```

### خطای SSL یا دامنه

```bash
getent hosts download.example.com
sudo certbot certificates
sudo nginx -t
```

### فایل‌های بزرگ متوقف می‌شوند

```bash
df -h
free -h
docker compose logs --tail=300 file-link-bot
docker compose logs --tail=300 telegram-bot-api
```

برای یک فایل ۳ گیگابایتی، هنگام انتقال چند برابر حجم فایل فضای آزاد در نظر بگیرید.

### بررسی reboot خودکار

```bash
sudo systemctl is-enabled docker
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' uploadrobotelemovie-file-link-bot-1
```

نام کانتینر ممکن است متفاوت باشد؛ نام صحیح را با دستور زیر پیدا کنید:

```bash
docker compose ps
```

## ۱۹. reboot و خاموش‌کردن سرور

راه‌اندازی مجدد سرور:

```bash
sudo reboot
```

بعد از اتصال مجدد، وضعیت ربات را بررسی کنید:

```bash
cd /opt/uploadrobotelemovie
docker compose ps
```

خاموش‌کردن سرور:

```bash
sudo shutdown -h now
```

بستن SSH یا ترمینال، ربات را متوقف نمی‌کند. خاموش‌کردن خود سرور، ربات را تا روشن‌شدن مجدد از دسترس خارج می‌کند.

## ۲۰. چک‌لیست امنیتی

- پورت `8080` فقط روی `127.0.0.1` منتشر شود.
- پورت `8081` مربوط به Local Telegram API روی اینترنت منتشر نشود.
- پورت `9090` پس از نصب بسته شود.
- فقط پورت‌های SSH، HTTP و HTTPS عمومی باشند.
- فایل `.env` و بکاپ‌ها با دسترسی `600` نگهداری شوند.
- توکن ربات را در پیام، issue یا مخزن GitHub قرار ندهید.
- بکاپ را روی یک سرور یا فضای ذخیره‌سازی جداگانه نیز نگه دارید.

بررسی پورت‌های عمومی:

```bash
sudo ss -lntp
sudo ufw status verbose
```

بررسی دسترسی فایل‌های حساس:

```bash
sudo ls -l /opt/uploadrobotelemovie/.env
sudo ls -lh /var/backups/uploadrobotelemovie
```
