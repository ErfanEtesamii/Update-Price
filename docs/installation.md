# نصب و راه‌اندازی Faraz-Honar به‌عنوان Windows Service (NSSM)

این سند نحوه‌ی اجرای دائمی «Faraz-Honar» (ربات تلگرامی آپدیت قیمت روی DigiKala، SnappShop، TapsiShop، Basalam و WooCommerce از روی Google Sheet) روی سرور لوکال ویندوزی را توضیح می‌دهد. روش کار همان روش پروژه‌ی «Local Marketplace Order Synchronization Platform» است: راه‌اندازی خودکار با بوت، ری‌استارت خودکار بعد از crash و لاگ‌های rotate‌شونده.

| مورد | مقدار |
|---|---|
| نام سرویس | `FarazHonarPriceUpdater` |
| دستور اجرا | `venv\Scripts\python.exe -m faraz_honar.main` |
| پوشه‌ی کاری (AppDirectory) | ریشه‌ی پروژه |
| لاگ برنامه | `logs\faraz-honar.log` |
| لاگ سرویس | `logs\service-stdout.log`، `logs\service-stderr.log` |
| حداقل پایتون | 3.11 |

## تفاوت‌های مهم با پروژه‌ی قبلی

- **پکیج واقعی:** باید با `pip install -e .` نصب شود، نه فقط `pip install -r requirements.txt`؛ در غیر این صورت `faraz_honar` قابل import نیست. آرگومان سرویس هم `-m faraz_honar.main` است (نه `-m src.main`).
- **لاگ فایل:** `main.py` قبلاً فقط `logging.basicConfig` به stderr داشت. حالا `src/faraz_honar/logger.py` لاگ را هم در `logs\faraz-honar.log` (rotate در ۱۰MB، ۵ نسخه‌ی پشتیبان) و هم روی stderr می‌نویسد.
- **مسیرهای نسبی:** `.env`، `config\sheet_mapping.yaml`، `data\` و `logs\` همگی نسبت به پوشه‌ی کاری خوانده می‌شوند؛ پس `AppDirectory` حتماً باید ریشه‌ی پروژه باشد (اسکریپت نصب این را تنظیم می‌کند).
- **فایل‌های غیرقابل commit:** `.env`، JSON سرویس‌اکانت گوگل (`config\faraz-honar-*.json`) و `config\sheet_mapping.yaml` باید دستی به سرور منتقل شوند.
- **توکن‌های DigiKala:** توکن‌های refresh‌شده در `data\tokens.json` و `data\tokens_asli.json` ذخیره می‌شوند. پوشه‌ی `data\` را هنگام جابه‌جایی/به‌روزرسانی پروژه پاک نکنید.

## مرحله ۱ — محیط اجرا

از ریشه‌ی پروژه (PowerShell یا CMD):

```bat
python -m venv venv
venv\Scripts\pip install -e .
venv\Scripts\python.exe -c "import faraz_honar"
```

دستور آخر باید بدون خطا تمام شود. ورژن پایتون سرور باید 3.11 یا بالاتر باشد.

## مرحله ۲ — پیکربندی

1. `.env.example` را به `.env` کپی کنید.
2. مقادیر زیر را پر کنید:
   - `DRY_RUN=true` (تا تأیید صریح کارفرما تغییر نکند)
   - `GOOGLE_SHEET_ID`، `GOOGLE_SHEET_WORKSHEET_GID` / `GOOGLE_SHEET_WORKSHEET_NAME`، `GOOGLE_SERVICE_ACCOUNT_FILE`
   - توکن‌های هر پلتفرم (DigiKala، SnappShop، TapsiShop، Basalam، WooCommerce)
   - `TELEGRAM_BOT_TOKEN` و `TELEGRAM_ALLOWED_USER_IDS`
3. `PRODUCT_SOURCE_CSV_PATH` روی سرور پروداکشن **خالی** بماند (فقط fallback تست است).
4. فایل JSON سرویس‌اکانت و `config\sheet_mapping.yaml` را دستی روی سرور بگذارید.
5. مطمئن شوید `client_email` داخل JSON روی Google Sheet با دسترسی Viewer share شده است.

## مرحله ۳ — تست دستی قبل از سرویس‌سازی

```bat
venv\Scripts\python.exe -m faraz_honar.main
```

- در کنسول باید خطوط `Starting Faraz Honar price updater (dry_run=True)` و `Bot ready. Available platforms: ...` دیده شود.
- recheck ساعتی محصولات محدودشده با قانون ۱۲٪ به‌صورت پیش‌فرض **خاموش** است (`HOURLY_RECHECK_ENABLED=false`). فقط اگر آن را `true` کرده باشید، خط `Scheduled hourly 12% recheck ...` باید ظاهر شود؛ اگر به‌جای آن هشدار «No JobQueue available» دیدید، recheck ساعتی اجرا نمی‌شود (مشکل نصب APScheduler).
- به ربات در تلگرام `/start` بدهید و مطمئن شوید پاسخ می‌دهد.
- **هیچ نمونه‌ی دیگری با همین `TELEGRAM_BOT_TOKEN` نباید در حال اجرا باشد** (لپ‌تاپ دولوپر، سرور قبلی و ...). `run_polling()` فقط یک نمونه را می‌پذیرد و در غیر این صورت خطای `Conflict: terminated by other getUpdates request` می‌گیرید و سرویس بعد از هر ری‌استارت دوباره همین خطا را می‌دهد.
- با `Ctrl+C` متوقف کنید و `logs\faraz-honar.log` را بررسی کنید.

## مرحله ۴ — نصب سرویس

1. فایل `nssm.exe` (نسخه‌ی win64، از پروژه‌ی قبلی یا nssm.cc) را در `deploy\` بگذارید.
2. یک CMD **با دسترسی Administrator** باز کنید:

```bat
deploy\install_service.bat
```

اسکریپت پیش از نصب موارد زیر را چک می‌کند: دسترسی ادمین، وجود `nssm.exe`، `venv`، `.env`، `sheet_mapping.yaml` و قابل import بودن `faraz_honar`. تنظیمات اعمال‌شده روی سرویس:

| تنظیم | مقدار |
|---|---|
| Application / AppParameters | `venv\Scripts\python.exe` / `-m faraz_honar.main` |
| AppDirectory | ریشه‌ی پروژه |
| Start | `SERVICE_AUTO_START` |
| AppExit Default | `Restart` |
| AppRestartDelay | `5000` (ms) |
| AppStopMethodConsole | `15000` (ms، فرصت shutdown تمیز) |
| AppEnvironmentExtra | `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` (برای لاگ فارسی) |
| AppStdout / AppStderr | `logs\service-stdout.log` / `logs\service-stderr.log` |
| AppRotateFiles / AppRotateOnline / AppRotateBytes | `1` / `1` / `10485760` |

اسکریپت نصب سرویس را **استارت نمی‌کند**؛ اسکریپت را می‌توان دوباره اجرا کرد (سرویس موجود دوباره پیکربندی می‌شود).

## مرحله ۵ — تست سرویس

```bat
deploy\nssm.exe start FarazHonarPriceUpdater
deploy\nssm.exe status FarazHonarPriceUpdater
```

چک‌لیست:

- [ ] `services.msc` → وضعیت `Running` و Startup Type = `Automatic`
- [ ] در `logs\\faraz-honar.log` خطوط `Starting Faraz Honar price updater (dry_run=True)` و `Bot ready` هست (خط `Scheduled hourly 12% recheck` فقط وقتی `HOURLY_RECHECK_ENABLED=true` باشد)
- [ ] در `logs\service-stderr.log` خطای Traceback نیست
- [ ] ربات تلگرام به `/start` پاسخ می‌دهد
- [ ] (فقط اگر `HOURLY_RECHECK_ENABLED=true`) job ساعتی (`hourly_capped_price_recheck`) بدون لاگین‌بودن هیچ کاربری اجرا می‌شود: سرویس در Session 0 و بدون UI اجرا می‌شود و job داخل event loop ربات (JobQueue) است، پس به دسکتاپ وابسته نیست. برای اطمینان، یک بازه‌ی ساعتی صبر کنید (یا `HOURLY_RECHECK_MINUTES` را موقتاً کم کنید) و اجرای job را در لاگ ببینید.
- [ ] تست crash: پروسه‌ی `python.exe` سرویس را در Task Manager kill کنید؛ باید طی حدود ۵ ثانیه دوباره بالا بیاید.

ری‌استارت و حذف:

```bat
deploy\restart_service.bat
deploy\uninstall_service.bat
```

## مرحله ۶ — تست ری‌استارت سرور

سرور را واقعاً ری‌استارت کنید (بدون لاگین کردن هیچ کاربری) و بعد از بالا آمدن ویندوز بررسی کنید:

- سرویس خودش `Running` شده است.
- در `logs\faraz-honar.log` خط `Starting Faraz Honar...` با زمان بعد از بوت دیده می‌شود.
- ربات تلگرام به `/start` پاسخ می‌دهد (polling دوباره وصل شده است).

اگر شبکه هنگام بوت هنوز آماده نبود و سرویس یک‌بار fail شد، ری‌استارت خودکار (۵ ثانیه) آن را جبران می‌کند.

## چطور بدون `DRY_RUN=false` کردن دستی، سرویس را ایمن نگه داریم

- **پیش‌فرض ایمن است:** در `config.py` مقدار پیش‌فرض `DRY_RUN` برابر `true` است؛ یعنی حتی اگر خط `DRY_RUN` از `.env` حذف شود یا غلط‌املایی داشته باشد، سرویس در حالت امن می‌ماند. اسکریپت‌های deploy هرگز `DRY_RUN` را تغییر نمی‌دهند.
- **دقیقاً چه چیزی بلاک می‌شود:** طبق کد کلاینت‌ها، `DRY_RUN=true` فقط **نوشتن** قیمت را بلاک می‌کند (PUT/PATCH/POST آپدیت قیمت). درخواست‌های خواندنی (GET) به پلتفرم‌ها و Google Sheets واقعی انجام می‌شوند. (متن README پروژه در مورد DigiKala قدیمی است و می‌گوید خواندن هم بلاک می‌شود؛ کد فعلی این‌طور نیست.)
- **تأیید در لاگ و تلگرام:** هر بار استارت، خط `dry_run=True` در لاگ ثبت می‌شود و در هر اجرا بنر DRY_RUN در تلگرام نمایش داده می‌شود. قبل از تحویل، این دو را چک کنید.
- **Refresh توکن جدا کنترل می‌شود:** refresh توکن DigiKala فقط با `DIGIKALA_ENABLE_TOKEN_REFRESH=true` (و برای حساب اصلی `DIGIKALA_ASLI_ENABLE_TOKEN_REFRESH=true`) فعال می‌شود، مستقل از `DRY_RUN`. تا زمان تأیید، `false` بماند.
- **رفتن به حالت واقعی فقط با تصمیم کارفرما:** پس از تأیید صریح، فقط مقدار `DRY_RUN=false` را در `.env` عوض کنید و `deploy\restart_service.bat` را بزنید. تغییرات `.env` بدون ری‌استارت سرویس اعمال نمی‌شود. برای برگشت هم `DRY_RUN=true` و ری‌استارت.
- **حفاظت از فایل‌های حساس:** `.env` و JSON سرویس‌اکانت را commit نکنید. `.gitignore` باید شامل `.env`، `config/faraz-honar-*.json` و `logs/*.log` باشد. توکن ربات تلگرام در لاگ ثبت نمی‌شود (لاگ‌های `httpx` روی WARNING تنظیم شده‌اند)، اما هنوز لاگ‌ها را با افراد غیرمجاز به اشتراک نگذارید.

## عیب‌یابی سریع

| علامت | علت محتمل |
|---|---|
| سرویس مدام Start/Stop می‌شود | خطای پیکربندی (مثلاً `TELEGRAM_BOT_TOKEN` خالی) — پایان `logs\faraz-honar.log` را ببینید؛ برنامه با کد ۱ خارج و NSSM هر ۵ ثانیه دوباره اجرا می‌کند |
| `Conflict: terminated by other getUpdates request` | نمونه‌ی دیگری با همان توکن در حال اجراست |
| `ModuleNotFoundError: faraz_honar` | `pip install -e .` داخل `venv` انجام نشده |
| `FileNotFoundError` برای `config/sheet_mapping.yaml` یا `.env` | `AppDirectory` ریشه‌ی پروژه نیست؛ `install_service.bat` را دوباره اجرا کنید |
| خطای دسترسی به Google Sheet | Sheet با `client_email` سرویس‌اکانت share نشده |
| خطای 403 SnappShop | محدودیت IP؛ `SNAPPSHOP_PROXY_URL` را تنظیم کنید یا از IP ایران استفاده کنید |
