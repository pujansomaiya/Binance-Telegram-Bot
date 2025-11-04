from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Your AI bot is now live 🚀")

app = ApplicationBuilder().token("YOUR_TELEGRAM_BOT_TOKEN").build()
app.add_handler(CommandHandler("start", start))

app.run_polling()
