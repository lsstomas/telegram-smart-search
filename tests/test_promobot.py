import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tgsearch import promo_delivery, promo_menu, watch
from tgsearch.models import Message


class FakeBot:
    chat_id = 123

    def __init__(self):
        self.messages = []
        self.options = []

    def message(self, text, *, message_id=None, **options):
        self.messages.append((text, message_id))
        self.options.append(options)
        return {"message_id": message_id or len(self.messages)}

    def call(self, method, **options):
        pass


class PromoBotTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(promo_delivery.SCHEMA)
        promo_menu.init(self.db, seed_defaults=True)
        self.bot = FakeBot()
        self.offer = promo_delivery.Offer(
            "rx7600:store:GPU100",
            "RX 7600",
            "kabum",
            139900,
            ["R$ 1.399,00 no Pix"],
            ["GPU100"],
            3000,
            "https://example.com/produto",
            {"https://t.me/loja/1": "Canal A"},
            seller="Loja Oficial",
            payment_method="Pix",
            installments="10× de R$ 149,90 sem juros · R$ 1.499,00",
        )

    def accept(self, timestamp, post, **changes):
        offer = replace(self.offer, posts={f"https://t.me/loja/{post}": f"Canal {post}"}, **changes)
        with self.db:
            promo_delivery.deliver_offer(self.db, self.bot, offer, timestamp)

    def test_edits_restart_the_hour_and_reposts_require_fresh_links(self):
        self.accept(0, 1)
        self.accept(3000, 2)
        self.accept(3900, 3)
        self.accept(8000, 3)
        self.accept(8000, 4)
        self.assertEqual([row[1] for row in self.bot.messages], [None, 1, 1, None])
        self.assertIn("Canal 1", self.bot.messages[2][0])
        self.assertIn("Canal 3", self.bot.messages[2][0])

    def test_price_drop_and_freight_change_send_new_alerts(self):
        self.accept(0, 1)
        self.accept(10, 2, price=139000)
        self.accept(20, 3, price=138900, payments=["R$ 1.389,00 no Pix"])
        self.accept(30, 4, price=138900, shipping=None, payments=["R$ 1.389,00 no Pix"])
        self.accept(40, 5, price=138900, shipping=0, payments=["R$ 1.389,00 no Pix"])
        self.assertEqual([row[1] for row in self.bot.messages], [None, None, 2, None])
        self.assertIn("caiu R$ 10,00", self.bot.messages[1][0])
        self.assertIn("Frete grátis", self.bot.messages[3][0])

    def test_repost_after_hour_with_different_price_and_restock_and_seller_info(self):
        self.accept(0, 1, seller="Vendedor A")
        # Different seller only -> edits existing notice within 1h, does not trigger new alert
        self.accept(100, 2, seller="Vendedor B")
        # Confirmed restock within 1h -> triggers new alert
        self.accept(200, 3, restocked=True)
        # Second channel posting same restock within 1h -> edits the restock message
        self.accept(300, 4, restocked=True)
        # After 1h, fresh post even at slightly different qualifying price -> new alert
        self.accept(4500, 5, price=140500, restocked=False)
        # Reprocessing post 5 even with big price drop -> ignored
        self.accept(4600, 5, price=120000, restocked=False)
        self.assertEqual([row[1] for row in self.bot.messages], [None, 1, None, 3, None])
        self.assertIn("voltou", self.bot.messages[2][0])

    def test_render_has_button_and_separate_lines_for_fields(self):
        self.accept(0, 1)
        text = self.bot.messages[0][0]
        opts = self.bot.options[0]
        self.assertIn("KaBuM", text)
        self.assertIn("Vendido por: Loja Oficial", text)
        self.assertIn("Preço: <b>R$ 1.399,00</b>", text)
        self.assertIn("Pagamento: Pix", text)
        self.assertIn("Parcelas: 10× de R$ 149,90 sem juros", text)
        self.assertIn("Cupom: <code>GPU100</code>", text)
        self.assertIn("Frete R$ 30,00", text)
        self.assertEqual(
            opts["reply_markup"],
            {"inline_keyboard": [[{"text": "Abrir oferta", "url": "https://example.com/produto"}]]},
        )

    def test_menu_creates_edits_pauses_reactivates_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite3"
            with sqlite3.connect(db_path) as db1:
                promo_menu.init(db1, seed_defaults=True)

                def message(text):
                    promo_menu.handle(
                        db1,
                        self.bot,
                        {"message": {"chat": {"type": "private", "id": 123}, "text": text}},
                    )

                promo_menu.handle_action(db1, self.bot, "new", 1)
                message("Air fryer")
                message("450,00")
                promo_menu.handle_action(db1, self.bot, "confirm", 2)
                alert = promo_menu.alerts(db1)[-1]
                self.assertEqual(
                    (alert.label, alert.ceiling, alert.paused), ("Air fryer", 45000, 0)
                )
                promo_menu.handle_action(db1, self.bot, f"cap:{alert.id}", 3)
                message("399,90")
                promo_menu.handle_action(db1, self.bot, f"toggle:{alert.id}:1", 4)
                db1.commit()
                self.assertEqual(len(promo_menu.alerts(db1, active_only=True)), 4)
                promo_menu.handle_action(db1, self.bot, f"toggle:{alert.id}:0", 5)
                db1.commit()

            # Reopen SQLite as if service restarted
            with sqlite3.connect(db_path) as db2:
                promo_menu.init(db2, seed_defaults=True)
                all_alerts = promo_menu.alerts(db2)
                self.assertEqual(len(all_alerts), 5)
                self.assertEqual(
                    [(a.product, a.ceiling) for a in all_alerts[:4]],
                    [("rx7600", 170000), ("rx7600xt", 170000), ("17e", 370000), ("17", 480000)],
                )
                saved = all_alerts[-1]
                self.assertEqual(
                    (saved.label, saved.ceiling, saved.paused), ("Air fryer", 39990, 0)
                )

    def test_offer_reads_payment_without_using_shipping_as_product_price(self):
        post = Message(
            "Canal",
            1,
            None,
            "Placa de vídeo RX 7600\nR$ 1.399,00 no Pix\n10x de R$ 149,90 sem juros\n"
            "Cupom GPU100\nFrete de R$ 30,00\nVendido por Kabum Oficial\n"
            "https://www.kabum.com.br/produto/123",
            link="https://t.me/loja/1",
        )
        alert = promo_menu.alerts(self.db)[0]
        (offer,) = watch.promo_matches(post, "channel", [(alert, watch.PROFILE)])
        self.assertEqual((offer.price, offer.shipping), (139900, 3000))
        self.assertEqual(offer.payment_method, "Pix")
        self.assertIn("10× de R$ 149,90 sem juros", offer.installments)
        self.assertTrue(any("10× de R$ 149,90 sem juros" in p for p in offer.payments))
        self.assertEqual(offer.coupons, ["GPU100"])
        self.assertEqual(offer.seller, "Kabum Oficial")
        self.assertEqual(watch.promo_shipping("Frete grátis para CEPs selecionados"), None)
        self.assertEqual(watch.promo_shipping("Frete a calcular no checkout"), None)

    def test_different_store_and_coupon_produce_distinct_keys(self):
        alert = promo_menu.alerts(self.db)[0]
        p1 = Message(
            "Canal A",
            1,
            None,
            "Placa de vídeo RX 7600\nR$ 1.399,00 no Pix\nCupom AAAA\nhttps://www.kabum.com.br/p/1",
            link="https://t.me/a/1",
        )
        p2 = Message(
            "Canal B",
            2,
            None,
            "Placa de vídeo RX 7600\nR$ 1.399,00 no Pix\nCupom BBBB\nhttps://www.kabum.com.br/p/1",
            link="https://t.me/b/2",
        )
        p3 = Message(
            "Canal C",
            3,
            None,
            "Placa de vídeo RX 7600\nR$ 1.399,00 no Pix\nCupom AAAA\nhttps://www.pichau.com.br/p/1",
            link="https://t.me/c/3",
        )
        (o1,) = watch.promo_matches(p1, "channel", [(alert, watch.PROFILE)])
        (o2,) = watch.promo_matches(p2, "channel", [(alert, watch.PROFILE)])
        (o3,) = watch.promo_matches(p3, "channel", [(alert, watch.PROFILE)])
        self.assertEqual(len({o1.key, o2.key, o3.key}), 3)

    def test_fresh_install_starts_without_personal_defaults(self):
        with sqlite3.connect(":memory:") as fresh_db:
            promo_menu.init(fresh_db)
            self.assertEqual(promo_menu.alerts(fresh_db), [])
            promo_menu.handle_action(fresh_db, self.bot, "list", 1)
            self.assertIn("ainda não tem alertas", self.bot.messages[-1][0])

    def test_real_iphone_17_without_lacrado_matches_and_dedupes(self):
        alert = next(a for a in promo_menu.alerts(self.db) if a.product == "17")
        post = Message(
            "Show de Ofertas 2.0",
            196564,
            None,
            "É PRA COMPRAAAAAAA AGORAAAAAAA!\n\n"
            "iPhone 17 256 GB 8 GB Preto | Mercado Livre\n\n"
            "🔥Por: R$\u00a04.399,00\n\n"
            "🎟 Use o cupom: MADRUXPTMANA\n\n"
            "Link: https://meli.la/1rjr5ei",
            link="https://t.me/showofertas2/196564",
        )
        (offer,) = watch.promo_matches(post, "channel", [(alert, watch.PROMO_PHONE_PROFILE)])
        self.assertEqual(offer.title, "iPhone 17 256 GB")
        self.assertEqual(offer.store, "mercadolivre")
        self.assertEqual(offer.price, 439900)
        self.assertEqual(offer.coupons, ["MADRUXPTMANA"])
        self.assertEqual(offer.buy_url, "https://meli.la/1rjr5ei")

    def test_zero_width_brand_bare_por_price_and_link_amazon(self):
        alert = next(a for a in promo_menu.alerts(self.db) if a.product == "17e")
        p1 = Message(
            "Crowman",
            1,
            None,
            "Ap\u2060ple iPh\u2060one 17e 256GB Rosa\n"
            "De R$ 7.299 por R$ 3.650 no Pix\n"
            "Use o Cupom: 7CJMELI + ELETRO300\n"
            "https://link.amazon/B00001",
            link="https://t.me/crowman/1",
        )
        (o1,) = watch.promo_matches(p1, "channel", [(alert, watch.PROMO_PHONE_PROFILE)])
        self.assertEqual(o1.store, "amazon")
        self.assertEqual(o1.price, 365000)
        self.assertEqual(o1.coupons, ["7CJMELI", "ELETRO300"])

        p2 = Message(
            "Promos",
            2,
            None,
            "Apple iPhone 17e 256gb rosa\n"
            "❌ DE: 4999,00\n"
            "✅ POR: 3650,00 🔥\n"
            "https://meli.la/abc123",
            link="https://t.me/promos/2",
        )
        (o2,) = watch.promo_matches(p2, "channel", [(alert, watch.PROMO_PHONE_PROFILE)])
        self.assertEqual(o2.store, "mercadolivre")
        self.assertEqual(o2.price, 365000)

        used = Message(
            "Promos",
            3,
            None,
            "iPhone 17 256 GB seminovo vitrine\nPor R$ 3.500,00\nhttps://meli.la/used",
            link="https://t.me/promos/3",
        )
        self.assertEqual(
            list(watch.promo_matches(used, "channel", [(alert, watch.PROMO_PHONE_PROFILE)])), []
        )


if __name__ == "__main__":
    unittest.main()
