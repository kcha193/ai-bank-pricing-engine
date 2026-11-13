"""SEC experiment: does dispatch_alerts_safely log the Telegram token / webhook URL
when the exception text embeds the URL? Network is blocked by patching socket."""
import logging, socket, io
def _blocked(*a, **k):
    raise OSError("network blocked by audit harness")
socket.create_connection = _blocked
socket.socket.connect = lambda *a, **k: _blocked()
socket.getaddrinfo = lambda *a, **k: _blocked()

from balance_pricing_engine.pricing_intelligence import alerts

buf = io.StringIO()
h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.DEBUG)

alert = alerts.plain_alert("audit test", "")
cases = {
    "tg_case_trailing_newline": alerts.TelegramAlertSink("123456:SECRETTOKENabc\n", "42"),
    "tg_case_with_space": alerts.TelegramAlertSink("123456:SECRET TOKEN", "42"),
    "tg_case_clean_network_down": alerts.TelegramAlertSink("123456:SECRETTOKENclean", "42"),
    "webhook_url_with_space": alerts.WebhookAlertSink("https://hooks.example.invalid/services/T0/B0/SECRETWEBHOOK x"),
}
for name, sink in cases.items():
    buf.truncate(0); buf.seek(0)
    alerts.dispatch_alerts_safely([alert], sink, deadline_seconds=5)
    out = buf.getvalue()
    leaked = "SECRET" in out
    print(f"{name}: leaked_secret_in_log={leaked}")
    print("   log:", out.strip().replace("\n", " | ")[:300])
