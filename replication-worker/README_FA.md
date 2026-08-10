# Worker انتقال روی سرور ایران

این Worker روی سرور ایران اجرا می‌شود، از طریق HTTPS به کنترلر وصل می‌شود و هیچ پورت ورودی نیاز ندارد. فایل از فضای اصلی دریافت و به فضای ایرانی منتقل می‌شود. دانلود نیمه‌تمام در `data/` می‌ماند و پس از restart ادامه پیدا می‌کند.

## نصب

1. در پنل اصلی به «تنظیمات ← اتصال Worker ایران» بروید و توکن را بردارید.
2. پوشه `replication-worker` را روی سرور ایران کپی کنید.
3. اجرا کنید:

```bash
cd replication-worker
chmod +x install.sh
sudo ./install.sh
```

در JSON نصب باید تنظیمات هر دو فضای مبدا و مقصد با همان `name` ثبت‌شده در پنل اصلی وارد شود. فایل `.env` دارای کلید محرمانه است و با مجوز `600` ساخته می‌شود.

## کنترل سرویس

```bash
docker compose ps
docker compose logs --tail=100 -f replication-worker
docker compose restart replication-worker
docker compose up -d --build
```

اگر Worker بیش از ۱۰ دقیقه از دسترس خارج شود، کنترلر مسیر آن را آزاد می‌کند تا Worker داخلی صف را متوقف نکند. با بازگشت Worker، فایل نیمه‌تمام موجود از همان byte ادامه داده می‌شود.
