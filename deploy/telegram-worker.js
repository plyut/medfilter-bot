// Cloudflare Worker: пересылает запросы бота на api.telegram.org.
// Нужен, когда сервер бота не может обратиться к Telegram напрямую.
// Адрес воркера (https://<имя>.<аккаунт>.workers.dev) вписывается в TELEGRAM_API_URL в .env.
export default {
  async fetch(request) {
    const url = new URL(request.url);
    // Пропускаем только запросы Bot API: /bot<токен>/<метод> и /file/bot<токен>/<путь>
    if (!url.pathname.startsWith("/bot") && !url.pathname.startsWith("/file/bot")) {
      return new Response("Not found", { status: 404 });
    }
    url.protocol = "https:";
    url.hostname = "api.telegram.org";
    url.port = "";
    return fetch(new Request(url, request));
  },
};
