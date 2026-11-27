import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from tgsearch import promo_delivery, promo_history, promo_menu, watch
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

    def test_30d_edge_cases_and_configurable_filters(self):
        # 1. Line 1 regular price + Line 2 deal price (conflicting_amounts recovery)
        rx = next(a for a in promo_menu.alerts(self.db) if a.product == "rx7600")
        p_dual = Message(
            "Crowman",
            10,
            None,
            "🔥 Placa De Vídeo Asrock Rx 7600 Challenger Pro Oc Amd Radeon R$ 1.989\n"
            "💵 R$ 1651\n"
            "🎟 Cupom: MODOTECH\n"
            "https://meli.la/1uNPNab",
            link="https://t.me/crowman/10",
        )
        (o_dual,) = watch.promo_matches(p_dual, "channel", [(rx, watch.profile_for_alert(rx))])
        self.assertEqual(o_dual.price, 165100)

        # 2. "DESCONTO NA FINALIZAÇÃO" after price is a checkout-discounted price, not discount amt
        p_fin = Message(
            "Talk PC",
            11,
            None,
            "🔥 Placa de Vídeo ASRock RX 7600 Challenger PRO OC AMD Radeon, 8GB\n"
            "💰 R$ 1647 DESCONTO NA FINALIZAÇÃO\n"
            "https://s.shopee.com.br/abc",
            link="https://t.me/talkpc/11",
        )
        (o_fin,) = watch.promo_matches(p_fin, "channel", [(rx, watch.profile_for_alert(rx))])
        self.assertEqual(o_fin.price, 164700)

        # 3. Joke headline mentioning old iPhone 6 + real iPhone 17e 256 GB deal
        p17e = next(a for a in promo_menu.alerts(self.db) if a.product == "17e")
        p_joke = Message(
            "Caça ofertas",
            12,
            None,
            "O IPHONE 6 DEPOIS DE TOMAR BOMBA\n"
            "📱 *Apple iPhone 17e 256 GB (branco e preto)*\n"
            "🔥 ~DE 5.729~ | *POR 3.699 no Pix*\n"
            "🎟 _Resgate o cupom: SMART10_\n"
            "🔗 https://link.amazon/abc",
            link="https://t.me/caca/12",
        )
        (o_joke,) = watch.promo_matches(p_joke, "channel", [(p17e, watch.profile_for_alert(p17e))])
        self.assertEqual((o_joke.store, o_joke.price), ("amazon", 369900))

        # 4. Configurable filters in Telegram menu: allow cashback + add excluded word
        p_cash = Message(
            "Canal",
            13,
            None,
            "iPhone 17e 256 GB Rosa com cashback\nR$ 3.600,00 no Pix\nhttps://meli.la/c1",
            link="https://t.me/canal/13",
        )
        # Blocked by default (block_restrictions=True)
        self.assertEqual(
            list(watch.promo_matches(p_cash, "channel", [(p17e, watch.profile_for_alert(p17e))])),
            [],
        )
        # Disable block_restrictions via menu callback
        promo_menu.handle_action(self.db, self.bot, f"flt:{p17e.id}:block_restrictions:0", 10)
        p17e_upd = next(a for a in promo_menu.alerts(self.db) if a.id == p17e.id)
        (o_cash,) = watch.promo_matches(
            p_cash, "channel", [(p17e_upd, watch.profile_for_alert(p17e_upd))]
        )
        self.assertEqual(o_cash.price, 360000)

        # Add "rosa" to excluded words via menu
        promo_menu.handle_action(self.db, self.bot, f"flt_words:{p17e.id}:exclude", 11)
        promo_menu.handle(
            self.db,
            self.bot,
            {"message": {"chat": {"type": "private", "id": 123}, "text": "rosa, usado"}},
        )
        p17e_exc = next(a for a in promo_menu.alerts(self.db) if a.id == p17e.id)
        self.assertEqual(
            list(
                watch.promo_matches(
                    p_cash, "channel", [(p17e_exc, watch.profile_for_alert(p17e_exc))]
                )
            ),
            [],
        )

        # Reset filters back to defaults
        promo_menu.handle_action(self.db, self.bot, f"flt_reset:{p17e.id}", 12)
        p17e_rst = next(a for a in promo_menu.alerts(self.db) if a.id == p17e.id)
        self.assertEqual(p17e_rst.filter_config()["exclude"], [])
        self.assertTrue(p17e_rst.filter_config()["block_restrictions"])

    def test_history_digest_groups_reposts_and_separates_ceiling(self):
        t1 = datetime(2026, 11, 27, 10, 26, tzinfo=timezone.utc)
        t2 = datetime(2026, 11, 27, 10, 29, tzinfo=timezone.utc)
        e1 = promo_history.HistoryEntry(
            offer=replace(
                self.offer,
                key='["17", "mercadolivre", ["MADRUXPTMANA"]]',
                title="iPhone 17 256 GB",
                store="mercadolivre",
                price=439900,
                coupons=["MADRUXPTMANA"],
                posts={"https://t.me/a/1": "Canal A"},
            ),
            seen_at=t1,
            within_ceiling=True,
            alert_ceiling=495000,
        )
        e2 = promo_history.HistoryEntry(
            offer=replace(e1.offer, posts={"https://t.me/b/2": "Canal B"}),
            seen_at=t2,
            within_ceiling=True,
            alert_ceiling=495000,
        )
        e_above = promo_history.HistoryEntry(
            offer=replace(
                e1.offer,
                key='["17", "amazon", []]',
                store="amazon",
                price=503000,
                coupons=[],
                posts={"https://t.me/c/3": "Canal C"},
            ),
            seen_at=t2,
            within_ceiling=False,
            alert_ceiling=495000,
        )
        e_above2 = promo_history.HistoryEntry(
            offer=replace(e_above.offer, price=503028, posts={"https://t.me/d/4": "Canal D"}),
            seen_at=t2,
            within_ceiling=False,
            alert_ceiling=495000,
        )
        chunks = promo_history.render_digest(
            "iPhone 17 256 GB",
            "últimas 24 horas",
            [e1, e2, e_above, e_above2],
            ceiling=495000,
        )
        self.assertEqual(len(chunks), 1)
        body = chunks[0]
        self.assertIn("Dentro do teto (1)", body)
        self.assertIn("<blockquote>🏆 <b>R$ 4.399,00</b>", body)
        self.assertIn("2 posts", body)
        self.assertIn("<blockquote expandable>📊 <b>Acima do teto", body)
        # R$ 5.030,00 and R$ 5.030,28 above ceiling merge into 1 entry
        self.assertIn("1 menores (de 1)", body)

        # Menu flow: hist_new & home_new always send NEW messages (message_id=None)
        promo_menu.handle_action(self.db, self.bot, "hist_new", 20)
        self.assertIsNone(self.bot.messages[-1][1])
        self.assertIn("Buscar histórico", self.bot.messages[-1][0])
        promo_menu.handle_action(self.db, self.bot, "home_new", 20)
        self.assertIsNone(self.bot.messages[-1][1])

        # Channel listing rendering
        ch_chunks = promo_history.render_channels(
            [
                promo_history.ChannelEntry("-1001", "Show de Ofertas", "showofertas2", "channel"),
                promo_history.ChannelEntry("-1002", "Canal Privado", "", "channel"),
            ]
        )
        self.assertIn("Canais monitorados (2)", ch_chunks[0])
        self.assertIn("@showofertas2", ch_chunks[0])
        self.assertIn("(privado)", ch_chunks[0])


if __name__ == "__main__":
    unittest.main()
