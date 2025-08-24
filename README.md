# SBALO Promo Bot — ENV версия (Render/VPS)

## ENV переменные
- BOT_TOKEN — токен из @BotFather  
- CHANNEL_USERNAME — @Sbalo_ru  
- SPREADSHEET_ID — ID Google Sheets  
- SERVICE_ACCOUNT_JSON — содержимое credentials.json (вся строка)  
- STAFF_IDS — ID кассиров через запятую (опц.)  
- SUBSCRIPTION_MIN_DAYS — минимальный стаж подписки (опц.)  

## Локальный запуск
```bash
pip install -r requirements.txt
$env:BOT_TOKEN="..." ; $env:CHANNEL_USERNAME="@Sbalo_ru"
$env:SPREADSHEET_ID="..." ; $env:SERVICE_ACCOUNT_JSON=(Get-Content credentials.json -Raw)
python main.py
